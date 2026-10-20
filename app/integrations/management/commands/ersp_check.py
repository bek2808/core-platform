"""
Проверка связи с ЕРСП: вход по данным из секретов и (по желанию) запрос
к провайдеру по ИНН. Токен на экран не выводится - только его тип и срок.

На сервере (из ~/core-platform):
    bin/core-run podman exec core-web python manage.py ersp_check
    bin/core-run podman exec core-web python manage.py ersp_check --env prod --inn 207078596

Каждый запрос попадает в журнал обмена (админ-панель: «Интеграции»).
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

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
            "--env", choices=settings.ERSP_ENVIRONMENTS, action="append",
            help="Среда (можно дважды). По умолчанию - обе: prod и sandbox.",
        )
        parser.add_argument(
            "--inn", help="ИНН организации: после входа запросить её у провайдера.",
        )

    def handle(self, *args, **options):
        environments = options["env"] or list(settings.ERSP_ENVIRONMENTS)
        failed = False
        for environment in environments:
            try:
                client = ErspClient(environment)
                info = client.token_info()
            except ErspError as error:
                failed = True
                self.stdout.write(self.style.ERROR(f"{environment}: вход не выполнен - {error.message}"))
                continue
            self.stdout.write(self.style.SUCCESS(
                f"{environment}: вход выполнен (секрет {client.secret_name}); "
                f"токен {info.get('token_type') or '?'}, действует {duration(info.get('expires_in'))}"
                + (f", scope: {info['scope']}" if info.get("scope") else "")
            ))
            if options["inn"]:
                try:
                    result = client.provider_inn(options["inn"])
                except ErspError as error:
                    failed = True
                    self.stdout.write(self.style.ERROR(f"{environment}: провайдер - {error.message}"))
                    continue
                self.stdout.write(
                    f"{environment}: провайдер ответил - {result.get('name') or '(без наименования)'}; "
                    f"полей в ответе: {len(result)}"
                )
        if failed:
            raise CommandError("Проверка не пройдена - подробности в журнале обмена.")
