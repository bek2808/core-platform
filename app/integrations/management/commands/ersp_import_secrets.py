"""
Разовый перенос данных для входа в ЕРСП из секретов Podman в карточку
подключения страховой (решение Бекзода 20.10).

До 20.10 данные для входа Mosaic лежали в секретах ersp_prod_credentials
и ersp_sandbox_credentials (заводил bin/ersp-secret). Теперь они хранятся
в базе зашифрованными и правятся на экране «Интеграции -> Подключение к ЕРСП».
Команда читает оба секрета и записывает их в карточку страховой.

На сервере (из ~/core-platform):
    bin/core-run podman exec core-web python manage.py ersp_import_secrets --company mosaic

Безопасность:
- значения на экран не выводятся - только что перенесено;
- уже заполненный блок среды (хоть одно значение) не перезаписывается:
  повторный запуск ничего не меняет и не затрёт то, что ввели на экране;
- изменение пишется в журнал изменений от имени системы (пароли - ***).

После переноса и проверки секреты удаляются с сервера, а эта команда -
из репозитория.
"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from companies.models import InsuranceCompany
from integrations import ersp
from integrations.models import Environment, ErspConnection

KEYS = ("client_id", "client_secret", "username", "password")


class Command(BaseCommand):
    help = "Перенести данные для входа в ЕРСП из секретов Podman в карточку подключения страховой."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Краткий код страховой, например mosaic.")

    def handle(self, *args, **options):
        company = InsuranceCompany.objects.filter(code=options["company"]).first()
        if company is None:
            raise CommandError(f"Нет страховой с кратким кодом {options['company']!r}.")

        found = 0
        for environment in Environment.values:
            secret = f"ersp_{environment}_credentials"
            path = settings.SECRETS_DIR / secret
            if not path.is_file():
                self.stdout.write(f"{environment}: секрета {secret} нет - пропускаю.")
                continue
            found += 1
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                raise CommandError(f"Секрет {secret} - не JSON. Ничего не перенесено для {environment}.")
            missing = [key for key in KEYS if not isinstance(data, dict) or not data.get(key)]
            if missing:
                raise CommandError(f"В секрете {secret} нет значений: {', '.join(missing)}.")

            with transaction.atomic():
                connection = ErspConnection.for_company(company)
                credentials = connection.credentials.select_for_update().get(environment=environment)
                if len(credentials.missing()) < len(KEYS):  # хоть одно значение уже есть
                    self.stdout.write(f"{environment}: в карточке уже есть данные - не трогаю.")
                    continue
                credentials.client_id = data["client_id"]
                credentials.username = data["username"]
                credentials.set_secret("client_secret", data["client_secret"])
                credentials.set_secret("password", data["password"])
                credentials.save()  # система: в журнал без сотрудника
            ersp.forget_token(company, environment)
            self.stdout.write(self.style.SUCCESS(
                f"{environment}: перенесено в карточку «{company}» "
                f"(client_id и логин - открыто, client_secret и пароль - шифром)."
            ))

        if not found:
            raise CommandError("Секретов ersp_*_credentials в контейнере нет - переносить нечего.")
