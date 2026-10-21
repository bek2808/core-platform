"""
Команда загрузки справочников ЕРСП из файлов.

На сервере (из ~/core-platform):
    bin/core-run podman exec core-web python manage.py load_ersp_references

Повторный запуск безопасен: неизменённые записи не трогаются,
пропавшие из ЕРСП не удаляются, а помечаются.
"""
from django.core.management.base import BaseCommand, CommandError

from references.loader import DATA_DIR, LoadError, load_from_files


class Command(BaseCommand):
    help = (
        "Загрузить справочники ЕРСП (регионы, районы, формы собственности, "
        "страховщики, валюты, ОКЭД, типы местности, типы обязательного страхования, "
        "типы резидентства, страны) из файлов."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-dir",
            default=str(DATA_DIR),
            help="Папка с soato_regions.json и подпапкой ersp/ (по умолчанию - файлы в образе).",
        )

    def handle(self, *args, **options):
        try:
            results = load_from_files(options["data_dir"])
        except LoadError as error:
            raise CommandError(f"Загрузка отменена, в базе ничего не изменилось: {error}")
        for name, result in results.items():
            self.stdout.write(f"{name}: {result}")
        self.stdout.write(self.style.SUCCESS("Справочники ЕРСП загружены."))
