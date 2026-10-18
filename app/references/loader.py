"""
Загрузка справочников ЕРСП в базу.

Как работает загрузка:
- читаем ответ ЕРСП ({"error": 0, "result": [...]}) - сейчас из файла, позже из ЕРСП;
- каждую строку сверяем с базой по id ЕРСП: новой записи - создаём, изменённой -
  обновляем, неизменённую не трогаем;
- записи, которых нет в ответе, не удаляем, а помечаем is_present = False;
- все три справочника загружаются в одной транзакции: если хоть один файл
  с ошибкой, в базе не меняется ничего.

Загрузку можно запускать сколько угодно раз: повторный запуск с теми же
данными ничего не меняет.
"""
import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from .models import ErspInsurer, ErspOwnershipForm, ErspRegion

DATA_DIR = Path(__file__).resolve().parent / "data"


class LoadError(Exception):
    """Ответ ЕРСП нельзя загрузить: ошибка, неверный формат, повтор id."""


@dataclass
class SyncResult:
    """Итог загрузки одного справочника."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    hidden: int = 0  # пропали из ответа ЕРСП и помечены is_present = False

    def __str__(self):
        return (
            f"новых {self.created}, изменено {self.updated}, "
            f"без изменений {self.unchanged}, пропало из ЕРСП {self.hidden}"
        )


# --- Чтение ответов ЕРСП ---------------------------------------------------------

def read_ersp_response(path):
    """Строки справочника из файла с ответом ЕРСП."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LoadError(f"{path}: не удалось прочитать JSON ({error})") from error
    return ersp_result(payload, source=str(path))


def ersp_result(payload, source="ответ ЕРСП"):
    """Список строк из ответа ЕРСП с проверкой, что ответ успешный."""
    if not isinstance(payload, dict) or payload.get("error") != 0:
        message = payload.get("error_message") if isinstance(payload, dict) else ""
        raise LoadError(f"{source}: ЕРСП вернул ошибку ({message or 'нет описания'})")
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise LoadError(f"{source}: в ответе нет списка result")
    return rows


def read_soato_regions(path=DATA_DIR / "soato_regions.json"):
    """Наша таблица «id региона ЕРСП -> код области по СОАТО»."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {int(region_id): code for region_id, code in data["regions"].items()}


# --- Разбор строк ------------------------------------------------------------------

def _text(row, key, required=False):
    """Текстовое поле строки; пустое -> "" (или ошибка, если поле обязательное)."""
    value = row.get(key)
    if value is None or str(value).strip() == "":
        if required:
            raise LoadError(f"строка id={row.get('id')}: нет поля {key}")
        return ""
    return str(value)


def _row_id(row):
    row_id = row.get("id") if isinstance(row, dict) else None
    if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0:
        raise LoadError(f"строка без правильного id: {row!r}")
    return row_id


def region_fields(row, soato_regions):
    return {
        "name": _text(row, "name", required=True),
        "name_uz": _text(row, "nameUz"),
        "name_uzc": _text(row, "nameUzc"),
        "soato_code": soato_regions.get(_row_id(row)),
    }


def ownership_form_fields(row):
    return {"name": _text(row, "name", required=True)}


def insurer_fields(row):
    # ИНН: убираем пробелы; пустой ИНН хранится как NULL (у 12 страховщиков его нет).
    tin = _text(row, "tin").strip() or None
    stopped = _text(row, "licenseStoppedDate")
    try:
        stopped_date = datetime.date.fromisoformat(stopped) if stopped else None
    except ValueError as error:
        raise LoadError(f"страховщик id={row['id']}: неверная дата {stopped!r}") from error
    industry = row.get("industryId")
    return {
        "name": _text(row, "name", required=True),
        "tin": tin,
        "industry": industry if isinstance(industry, int) else None,
        "license_stopped_date": stopped_date,
    }


# --- Сверка с базой ----------------------------------------------------------------

def sync_reference(model, rows, to_fields, now=None):
    """
    Сверить один справочник с ответом ЕРСП. to_fields(row) -> поля модели.
    Вызывается внутри транзакции (см. sync_all).
    """
    now = now or timezone.now()
    parsed = {}
    for row in rows:
        row_id = _row_id(row)
        if row_id in parsed:
            raise LoadError(f"{model._meta.verbose_name_plural}: id {row_id} повторяется")
        parsed[row_id] = (row, to_fields(row))

    result = SyncResult()
    existing = {obj.id: obj for obj in model.objects.filter(id__in=parsed)}
    for row_id, (row, fields) in parsed.items():
        obj = existing.get(row_id)
        if obj is None:
            model.objects.create(id=row_id, raw=row, synced_at=now, **fields)
            result.created += 1
            continue
        changed = obj.raw != row or not obj.is_present or any(
            getattr(obj, name) != value for name, value in fields.items()
        )
        if not changed:
            result.unchanged += 1
            continue
        for name, value in fields.items():
            setattr(obj, name, value)
        obj.raw = row
        obj.is_present = True
        obj.synced_at = now
        obj.save()
        result.updated += 1

    result.hidden = (
        model.objects.filter(is_present=True)
        .exclude(id__in=parsed)
        .update(is_present=False, synced_at=now)
    )
    return result


def sync_all(regions, ownership_forms, insurers, soato_regions, now=None):
    """Загрузить три справочника ЕРСП одной транзакцией: всё или ничего."""
    now = now or timezone.now()
    # ИНН страховщика в базе уникален; повтор в ответе ЕРСП покажем понятной
    # ошибкой, а не падением базы посреди загрузки.
    tins = [str(row.get("tin")).strip() for row in insurers if isinstance(row, dict) and row.get("tin")]
    repeated = sorted({tin for tin in tins if tins.count(tin) > 1})
    if repeated:
        raise LoadError(f"страховщики: ИНН повторяется: {', '.join(repeated)}")
    with transaction.atomic():
        return {
            "регионы": sync_reference(
                ErspRegion, regions, lambda row: region_fields(row, soato_regions), now
            ),
            "формы собственности": sync_reference(
                ErspOwnershipForm, ownership_forms, ownership_form_fields, now
            ),
            "страховщики": sync_reference(ErspInsurer, insurers, insurer_fields, now),
        }


def load_from_files(data_dir=DATA_DIR):
    """Загрузить справочники из файлов в data_dir/ersp/."""
    ersp_dir = Path(data_dir) / "ersp"
    return sync_all(
        regions=read_ersp_response(ersp_dir / "regions.json"),
        ownership_forms=read_ersp_response(ersp_dir / "ownership_forms.json"),
        insurers=read_ersp_response(ersp_dir / "insurers.json"),
        soato_regions=read_soato_regions(Path(data_dir) / "soato_regions.json"),
    )
