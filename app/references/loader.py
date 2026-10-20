"""
Загрузка справочников ЕРСП в базу.

Как работает загрузка:
- читаем ответ ЕРСП ({"error": 0, "result": [...]}) - сейчас из файла, позже из ЕРСП;
- каждую строку сверяем с базой по id ЕРСП: новой записи - создаём, изменённой -
  обновляем, неизменённую не трогаем;
- записи, которых нет в ответе, не удаляем, а помечаем is_present = False;
- все справочники загружаются в одной транзакции: если хоть один файл
  с ошибкой, в базе не меняется ничего.

Загрузку можно запускать сколько угодно раз: повторный запуск с теми же
данными ничего не меняет.
"""
import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from .models import (
    ErspCurrency,
    ErspDistrict,
    ErspInsurer,
    ErspOked,
    ErspOwnershipForm,
    ErspRegion,
)

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


def _oked_id(row):
    """
    У ОКЭД id - код строкой: обычно 5 цифр, с ведущим нулём (01452).
    В ЕРСП есть и коды верхних уровней классификатора - 2-4 цифры
    (73, 682, 9411: группа или класс, а 94110 - его подкласс). Храним как есть.
    """
    row_id = row.get("id") if isinstance(row, dict) else None
    if not isinstance(row_id, str) or not re.fullmatch(r"[0-9]{2,5}", row_id.strip()):
        raise LoadError(f"ОКЭД: строка без правильного кода: {row!r}")
    return row_id.strip()


def _clean(row, key, required=False):
    """Как _text, но без пробелов и переводов строк по краям (в ОКЭД бывает \\r)."""
    return _text(row, key, required).strip()


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


def district_fields(row, region_ids):
    region_id = row.get("regionId")
    if region_id not in region_ids:
        raise LoadError(f"район id={row.get('id')}: регион {region_id!r} не найден в регионах ЕРСП")
    return {"name": _clean(row, "name", required=True), "region_id": region_id}


def currency_fields(row):
    return {
        "ticker": _clean(row, "ticker"),
        "name": _clean(row, "name", required=True),
        "name_uz": _clean(row, "nameUz"),
        "name_uzc": _clean(row, "nameUzc"),
    }


def oked_fields(row):
    return {
        "name": _clean(row, "name", required=True),
        "name_uz": _clean(row, "nameUz"),
        "name_uzc": _clean(row, "nameUzc"),
    }


# --- Сверка с базой ----------------------------------------------------------------

def sync_reference(model, rows, to_fields, now=None, id_of=_row_id, raw_of=None):
    """
    Сверить один справочник с ответом ЕРСП. to_fields(row) -> поля модели,
    id_of(row) -> id записи, raw_of(row) -> что хранить в raw (по умолчанию
    строка целиком). Вызывается внутри транзакции (см. sync_all).
    """
    now = now or timezone.now()
    parsed = {}
    for row in rows:
        row_id = id_of(row)
        if row_id in parsed:
            raise LoadError(f"{model._meta.verbose_name_plural}: id {row_id} повторяется")
        parsed[row_id] = (raw_of(row) if raw_of else row, to_fields(row))

    result = SyncResult()
    # pk, а не id: у перечня ЦБ ключ - МФО (CbuBank.mfo).
    existing = {obj.pk: obj for obj in model.objects.filter(pk__in=parsed)}
    for row_id, (row, fields) in parsed.items():
        obj = existing.get(row_id)
        if obj is None:
            model.objects.create(pk=row_id, raw=row, synced_at=now, **fields)
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
        .exclude(pk__in=parsed)
        .update(is_present=False, synced_at=now)
    )
    return result


def sync_all(
    regions, ownership_forms, insurers, soato_regions, districts, currencies, okeds, now=None
):
    """Загрузить справочники ЕРСП одной транзакцией: всё или ничего."""
    now = now or timezone.now()
    # ИНН страховщика в базе уникален; повтор в ответе ЕРСП покажем понятной
    # ошибкой, а не падением базы посреди загрузки.
    tins = [str(row.get("tin")).strip() for row in insurers if isinstance(row, dict) and row.get("tin")]
    repeated = sorted({tin for tin in tins if tins.count(tin) > 1})
    if repeated:
        raise LoadError(f"страховщики: ИНН повторяется: {', '.join(repeated)}")
    with transaction.atomic():
        results = {
            "регионы": sync_reference(
                ErspRegion, regions, lambda row: region_fields(row, soato_regions), now
            ),
            "формы собственности": sync_reference(
                ErspOwnershipForm, ownership_forms, ownership_form_fields, now
            ),
            "страховщики": sync_reference(ErspInsurer, insurers, insurer_fields, now),
        }
        known_regions = set(ErspRegion.objects.values_list("id", flat=True))
        results |= {
            # Районы - после регионов: каждый район ссылается на свой регион.
            # Сверяем с регионами в базе (а не только в этом ответе): регион,
            # пропавший из ЕРСП, остаётся в базе с отметкой и может быть у района.
            "районы": sync_reference(
                ErspDistrict,
                districts,
                lambda row: district_fields(row, known_regions),
                now,
            ),
            "валюты": sync_reference(ErspCurrency, currencies, currency_fields, now),
            "ОКЭД": sync_reference(ErspOked, okeds, oked_fields, now, id_of=_oked_id),
        }
        return results


def load_from_files(data_dir=DATA_DIR):
    """Загрузить справочники из файлов в data_dir/ersp/."""
    ersp_dir = Path(data_dir) / "ersp"
    return sync_all(
        regions=read_ersp_response(ersp_dir / "regions.json"),
        ownership_forms=read_ersp_response(ersp_dir / "ownership_forms.json"),
        insurers=read_ersp_response(ersp_dir / "insurers.json"),
        soato_regions=read_soato_regions(Path(data_dir) / "soato_regions.json"),
        districts=read_ersp_response(ersp_dir / "districts.json"),
        currencies=read_ersp_response(ersp_dir / "currencies.json"),
        okeds=read_ersp_response(ersp_dir / "okeds.json"),
    )
