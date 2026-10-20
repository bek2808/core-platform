"""
Проверка связи с ЕРСП: вход по данным из карточки подключения страховой
и (по желанию) запрос к провайдеру по ИНН. Токен на экран не выводится -
только его тип и срок. То же делает кнопка «Проверить подключение» на экране
«Интеграции -> Подключение к ЕРСП».

На сервере (из ~/core-platform):
    bin/core-run podman exec core-web python manage.py ersp_check
    bin/core-run podman exec core-web python manage.py ersp_check --company mosaic --env prod --inn 207078596

Без --company - все страховые, у которых есть карточка подключения.
Каждый запрос попадает в журнал обмена (админ-панель: «Интеграции»).
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from companies.models import InsuranceCompany
from integrations.ersp import ErspClient, ErspError


def duration(seconds):
    if not seconds:
        return "срок не указан"
    hours, rest = divmod(int(seconds), 3600)
    days, hours = divmod(hours, 24)
    parts = [f"{days} дн." if days else "", f"{hours} ч" if hours else "", f"{rest // 60} мин" if rest // 60 else ""]
    return f"{int(seconds)} с ({' '.join(p for p in parts if p) or 'меньше минуты'})"


class Command(BaseCommand):
    help = "Проверить вход в ЕРСП (и, по желанию, запрос к провайдеру по ИНН)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", action="append",
            help="Краткий код страховой (можно несколько раз). По умолчанию - все с карточкой подключения.",
        )
        parser.add_argument(
            "--env", choices=settings.ERSP_ENVIRONMENTS, action="append",
            help="Среда (можно дважды). По умолчанию - обе: prod и sandbox.",
        )
        parser.add_argument(
            "--inn", help="ИНН организации: после входа запросить её у провайдера.",
        )

    def handle(self, *args, **options):
        environments = options["env"] or list(settings.ERSP_ENVIRONMENTS)
        if options["company"]:
            companies = []
            for code in options["company"]:
                company = InsuranceCompany.objects.filter(code=code).first()
                if company is None:
                    raise CommandError(f"Нет страховой с кратким кодом {code!r}.")
                companies.append(company)
        else:
            companies = list(InsuranceCompany.objects.filter(ersp_connection__isnull=False).order_by("code"))
            if not companies:
                raise CommandError(
                    "Ни у одной страховой нет карточки подключения к ЕРСП "
                    "(Интеграции -> Подключение к ЕРСП)."
                )

        failed = False
        for company in companies:
            for environment in environments:
                where = f"{company.code} {environment}"
                try:
                    client = ErspClient(environment, company)
                    info = client.token_info()
                except ErspError as error:
                    failed = True
                    self.stdout.write(self.style.ERROR(f"{where}: вход не выполнен - {error.message}"))
                    continue
                self.stdout.write(self.style.SUCCESS(
                    f"{where}: вход выполнен; "
                    f"токен {info.get('token_type') or '?'}, действует {duration(info.get('expires_in'))}"
                    + (f", scope: {info['scope']}" if info.get("scope") else "")
                ))
                if options["inn"]:
                    try:
                        result = client.provider_inn(options["inn"])
                    except ErspError as error:
                        failed = True
                        self.stdout.write(self.style.ERROR(f"{where}: провайдер - {error.message}"))
                        continue
                    self.stdout.write(
                        f"{where}: провайдер ответил - {result.get('name') or '(без наименования)'}; "
                        f"полей в ответе: {len(result)}"
                    )
        if failed:
            raise CommandError("Проверка не пройдена - подробности в журнале обмена.")
