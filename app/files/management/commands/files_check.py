"""
Проверка хранилища файлов (docs/files-model.md): всё ли на месте и цело ли.

На сервере (из ~/core-platform):
    bin/core-run podman exec core-web python manage.py files_check

Находит:
- записи, у которых нет файла на диске (потеря файла);
- файлы, у которых отпечаток SHA-256 не совпадает с записью (порча или подмена);
- файлы на диске без записи (загрузка оборвалась между записью файла и записью
  в базе) - только показывает, ничего не удаляет.
Первые два - ошибка (код выхода 1), третье - предупреждение.
"""
from django.core.management.base import BaseCommand, CommandError

from files import storage
from files.models import StoredFile


class Command(BaseCommand):
    help = "Проверить хранилище файлов: пропавшие, испорченные и лишние файлы."

    def handle(self, *args, **options):
        base = storage.root()
        missing, damaged, known = [], [], set()
        total = 0
        for stored in StoredFile.objects.order_by("uploaded_at").iterator():
            total += 1
            known.add(stored.path)
            try:
                path = storage.open_path(stored)
            except FileNotFoundError:
                missing.append(stored)
                continue
            if path.stat().st_size != stored.size or storage.sha256_of(path) != stored.sha256:
                damaged.append(stored)

        extra = []
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(base)
                if relative.parts[0] == storage.TMP_DIR:
                    continue
                if str(relative) not in known:
                    extra.append(str(relative))

        self.stdout.write(f"Записей о файлах: {total}")
        for stored in missing:
            self.stdout.write(self.style.ERROR(f"НЕТ ФАЙЛА: {stored.pk} {stored.original_name} ({stored.path})"))
        for stored in damaged:
            self.stdout.write(self.style.ERROR(
                f"НЕ СОВПАДАЕТ ОТПЕЧАТОК: {stored.pk} {stored.original_name} ({stored.path})"))
        for relative in extra:
            self.stdout.write(self.style.WARNING(f"ЛИШНИЙ ФАЙЛ (нет записи в базе): {relative}"))
        if missing or damaged:
            raise CommandError(
                f"Проблемы: пропало {len(missing)}, испорчено {len(damaged)}. "
                "Восстановите файлы из резервной копии (files.tar)."
            )
        suffix = f" Лишних файлов: {len(extra)}." if extra else ""
        self.stdout.write(self.style.SUCCESS(f"OK: все файлы на месте, отпечатки совпадают.{suffix}"))
