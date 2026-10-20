"""
Загрузка перечня банков Центрального банка (cbu.uz) и сверка с нашим
справочником банков (решения Бекзода 21.10).

На сервере (из ~/core-platform):
    bin/core-run podman exec core-web python manage.py load_cbu_banks

Что делает:
1. Находит файл «Филиалы коммерческих банков» на странице ЦБ и скачивает его
   (или берёт готовый: --file - < файл.xlsx, если сайт ЦБ недоступен).
2. Обновляет таблицу «Банки по перечню ЦБ» (references.CbuBank).
3. В нашем справочнике банков дописывает из ЦБ пустой ИНН и название вместо
   заглушки «Банк <МФО>»; то, что ввели люди, не трогает. Расхождения ИНН
   и банки, которых нет в перечне ЦБ, только показывает.
Всё в одной транзакции: ошибка в файле - в базе не меняется ничего.
Повторный запуск с тем же файлом ничего не меняет.
"""
import sys
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from counterparties import banks
from references import cbu
from references.loader import LoadError
from references.models import CbuBank


def mfo_list(items, limit=20):
    shown = ", ".join(items[:limit])
    return shown + (f" и ещё {len(items) - limit}" if len(items) > limit else "")


class Command(BaseCommand):
    help = "Загрузить перечень банков ЦБ (cbu.uz) и дописать ИНН в справочник банков."

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            help="Файл ЦБ (.xlsx) вместо скачивания; «-» - читать из стандартного ввода "
                 "(podman exec -i core-web python manage.py load_cbu_banks --file - < файл.xlsx).",
        )

    def handle(self, *args, **options):
        try:
            if options["file"] == "-":
                source, content = "стандартный ввод", sys.stdin.buffer.read()
            elif options["file"]:
                source, content = options["file"], Path(options["file"]).read_bytes()
            else:
                source, content = cbu.download()
            rows = cbu.read_rows(content)
            with transaction.atomic():
                result = cbu.sync(rows)
                report = banks.fill_all_from_cbu()
        except (LoadError, OSError) as error:
            raise CommandError(f"Загрузка отменена, в базе ничего не изменилось: {error}")

        kinds = Counter(row["kind"] for row in rows)
        by_kind = ", ".join(
            f"{CbuBank.Kind(kind).label if kind in CbuBank.Kind.values else f'вид {kind}'} - {count}"
            for kind, count in sorted(kinds.items())
        )
        self.stdout.write(f"Файл ЦБ: {source}")
        self.stdout.write(f"Строк с банками: {len(rows)} ({by_kind}); с ИНН: {sum(1 for r in rows if r['inn'])}.")
        self.stdout.write(
            f"Перечень ЦБ: новых {result.created}, изменено {result.updated}, "
            f"без изменений {result.unchanged}, пропало из перечня {result.hidden}."
        )
        self.stdout.write(
            f"Наш справочник банков: ИНН дописан из ЦБ - {len(report.inn_filled)}"
            + (f" ({mfo_list(report.inn_filled)})" if report.inn_filled else "")
            + f"; название вместо заглушки - {len(report.name_filled)}"
            + (f" ({mfo_list(report.name_filled)})" if report.name_filled else "")
            + "."
        )
        for mfo, ours, theirs in report.inn_differs:
            self.stdout.write(self.style.WARNING(
                f"ИНН отличается от ЦБ (не меняли): МФО {mfo} - у нас {ours}, в ЦБ {theirs}."
            ))
        if report.not_in_cbu:
            self.stdout.write(self.style.WARNING(
                f"Нет в перечне ЦБ (банк не выключали): МФО {mfo_list(report.not_in_cbu)}."
            ))
        self.stdout.write(self.style.SUCCESS("Перечень банков ЦБ загружен."))
