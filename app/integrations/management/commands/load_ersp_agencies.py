"""
Загрузить справочник ЕРСП «Агенты/филиалы» страховой.

Среда - та, куда страховая отправляет полисы (карточка «Интеграции ->
Подключение к ЕРСП», сейчас песочница): agencyId нужен для отправки,
а в песочнице и в боевой среде id разные.

На сервере (из ~/core-platform):
    bin/core-run podman exec core-web python manage.py load_ersp_agencies --company mosaic

После загрузки печатает все записи: agencyId, вид в ЕРСП, к кому
привязана, название и отметки - по ним головной офис и филиалы
связываются с ЕРСП на экране подразделения. Запрос попадает в журнал
обмена. Повторный запуск безопасен.
"""
from django.core.management.base import BaseCommand, CommandError

from companies.models import InsuranceCompany
from integrations.agencies import load_agencies
from integrations.ersp import ErspError
from integrations.models import ErspAgency
from references.loader import LoadError


class Command(BaseCommand):
    help = "Загрузить справочник ЕРСП «Агенты/филиалы» страховой (среда отправки полисов)."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Краткий код страховой, например mosaic.")

    def handle(self, *args, **options):
        company = InsuranceCompany.objects.filter(code=options["company"]).first()
        if company is None:
            raise CommandError(f"Нет страховой с кратким кодом {options['company']!r}.")
        try:
            environment, result = load_agencies(company)
        except ErspError as error:
            raise CommandError(f"Запрос к ЕРСП не удался, в базе ничего не изменилось: {error.message}")
        except LoadError as error:
            raise CommandError(f"Загрузка отменена, в базе ничего не изменилось: {error}")

        label = dict(ErspAgency._meta.get_field("environment").choices)[environment]
        self.stdout.write(f"{company.code}, среда «{label}»: {result}")
        rows = ErspAgency.objects.filter(insurance_company=company, environment=environment).order_by("ersp_id")
        for agency in rows:
            marks = [m for m, on in (("удалён", agency.is_deleted), ("нет в ЕРСП", not agency.is_present)) if on]
            parent = f" -> {agency.parent_ersp_id}" if agency.parent_ersp_id else ""
            kind = agency.get_agency_type_display() if agency.agency_type else "вид не указан"
            self.stdout.write(
                f"  {agency.ersp_id}{parent}: {kind}, {agency.name}"
                + (f", ИНН {agency.inn}" if agency.inn else "")
                + (f" [{', '.join(marks)}]" if marks else "")
            )
        self.stdout.write(self.style.SUCCESS("Справочник агентов ЕРСП загружен."))
