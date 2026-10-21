"""
Запись и чтение файлов хранилища (docs/files-model.md).

Единственный способ положить файл в систему - store(): она проверяет тип
по содержимому и размер, пишет файл под случайным именем и заводит запись
StoredFile. Читать - open_path() (путь проверяется: из папки хранилища
не выйти).

Как кладётся файл:
1. Загрузка пишется во временный файл в той же папке хранилища (.tmp),
   по ходу считается SHA-256 и размер; больше предела - загрузка
   обрывается, не дочитав.
2. Проверенный файл переносится на место одним действием (os.replace):
   наполовину записанного файла на месте не бывает.
3. Затем создаётся запись в базе. Не получилось - файл убирается.
   Если процесс оборвался между 2 и 3, на диске останется файл без записи:
   его найдёт команда files_check (удалять ничего не станет).
"""
import hashlib
import os
import re
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import registry
from .models import StoredFile

CHUNK = 64 * 1024
TMP_DIR = ".tmp"


class FileRejected(Exception):
    """Файл не принят; текст понятен человеку и показывается на экране."""


def root():
    return Path(settings.FILES_ROOT)


def open_path(stored):
    """Путь к файлу на диске. Бросает FileNotFoundError, если файла нет."""
    base = root().resolve()
    path = (base / stored.path).resolve()
    if base != path and base not in path.parents:
        raise FileNotFoundError(f"Путь файла выходит из хранилища: {stored.path}")
    if not path.is_file():
        raise FileNotFoundError(stored.path)
    return path


def clean_name(name, mime):
    """Исходное имя для показа и скачивания: без пути и управляющих знаков."""
    name = re.split(r"[\\/]", name or "")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    ext = registry.KINDS[mime][0]
    if not name:
        return f"file.{ext}"
    if len(name) > 255:
        stem, dot, tail = name.rpartition(".")
        keep = 255 - len(tail) - 1 if dot else 255
        name = f"{(stem or tail)[:keep]}.{tail}" if dot else name[:255]
    return name


def store(upload, *, purpose, owner_id, company, by=None):
    """
    Принять загруженный файл (upload - UploadedFile из request.FILES или
    любой объект с name и chunks()). Возвращает StoredFile.
    Бросает FileRejected с понятным текстом, если файл не подходит.
    """
    rule = registry.get(purpose)
    base = root()
    tmp_dir = base / TMP_DIR
    tmp_dir.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    size = 0
    head = b""
    fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, suffix=".part")
    tmp = Path(tmp_name)
    final = None
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in upload.chunks(CHUNK):
                size += len(chunk)
                if size > rule.max_size:
                    raise FileRejected(
                        f"Файл больше {rule.max_size // registry.MB} МБ. "
                        "Уменьшите его (для скана - сожмите PDF) и загрузите снова."
                    )
                if len(head) < registry.SIGNATURE_LENGTH:
                    head += chunk[: registry.SIGNATURE_LENGTH - len(head)]
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise FileRejected("Файл пустой.")
        mime = registry.sniff(head)
        if mime is None or mime not in rule.types:
            raise FileRejected(
                f"Этот файл не подходит: принимаются только {rule.allowed_names()}. "
                "Тип проверяется по содержимому, а не по окончанию имени."
            )

        now = timezone.now()
        ext = registry.KINDS[mime][0]
        relative = f"{company.pk}/{now:%Y}/{now:%m}/{uuid.uuid4().hex}.{ext}"
        final = base / relative
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)

        record = StoredFile(
            insurance_company=company, purpose=purpose, owner_id=owner_id,
            original_name=clean_name(getattr(upload, "name", ""), mime), content_type=mime,
            size=size, sha256=digest.hexdigest(), path=relative, uploaded_by=by, uploaded_at=now,
        )
        try:
            record.save()
        except Exception:
            final.unlink(missing_ok=True)
            raise
        return record
    finally:
        tmp.unlink(missing_ok=True)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
