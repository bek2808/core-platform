"""
Загрузка справочника ЕРСП «Агенты/филиалы» страховой (ErspAgency).

Откуда: GET /api/references/agencies в той среде, куда страховая
отправляет полисы (карточка «Интеграции -> Подключение к ЕРСП», сейчас
песочница): agencyId нужен именно для отправки, а id в средах разные.

Как работает (так же, как загрузка остальных справочников ЕРСП,
references/loader.py):
- строки сверяются с базой по id ЕРСП внутри своей страховой и среды:
  новую - создаём, изменённую - обновляем, неизменённую не трогаем;
- строки, которых нет в ответе, не удаляются, а получают отметку
  «нет в ЕРСП» (is_present = False);
- запись хранится как прислал ЕРСП (raw), ошибки ввода не исправляются
  (агент, заведённый филиалом, остаётся «филиалом»);
- загрузка - одна транзакция: при ошибке в ответе в базе не меняется
  ничего. Запрос к ЕРСП идёт ДО транзакции: запись о нём в журнале
  обмена остаётся, даже если разбор ответа не удался.

Повторный запуск с тем же ответом ничего не меняет.

Отметка удаления. Как именно ЕРСП отдаёт удалённые записи (кнопка
«Delete», статус «Удалён»), из описания метода не видно: в ответе боевой
среды, присланном Бекзодом 21.10, такого поля нет. Поэтому понимаем
любое из полей DELETION_KEYS; если ЕРСП удалённые просто не отдаёт,
запись получит отметку «нет в ЕРСП» - для связи она закрыта так же.
Первая настоящая загрузка покажет, как на самом деле (raw хранится).
"""
from django.db import transaction
from django.utils import timezone

from references.loader import LoadError, SyncResult

from .ersp import SEND, ErspClient
from .models import ErspAgency

# Поля, по которым ЕРСП может сообщать, что запись удалена (см. выше).
DELETION_KEYS = ("deletionMark", "isDeleted", "deleted", "isDeletionMark")
# Значения этих полей, которые означают «не удалена».
NOT_DELETED = (None, False, 0, "0", "", "false", "False", "active", "Активен")


def _text(value):
    return "" if value is None else str(value).strip()


def _positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _pinfl(row):
    """ПИНФЛ: у агента-физлица - в person (сверху или в passportData)."""
    person = row.get("person") if isinstance(row.get("person"), dict) else {}
    passport = person.get("passportData") if isinstance(person.get("passportData"), dict) else {}
    return _text(row.get("pinfl") or person.get("pinfl") or passport.get("pinfl"))


def is_deleted(row):
    return any(key in row and row[key] not in NOT_DELETED for key in DELETION_KEYS)


def agency_fields(row):
    """Поля ErspAgency из строки ответа ЕРСП."""
    ersp_id = _positive_int(row.get("id") if isinstance(row, dict) else None)
    if ersp_id is None:
        raise LoadError(f"агенты ЕРСП: строка без правильного id: {row!r}")
    name = _text(row.get("name"))
    if not name:
        raise LoadError(f"агенты ЕРСП: у записи {ersp_id} нет названия")
    agency_type = row.get("agencyType")
    return ersp_id, {
        "name": name,
        "inn": _text(row.get("inn")),
        "pinfl": _pinfl(row),
        "agency_type": agency_type if agency_type in ErspAgency.AgencyType.values else None,
        "parent_ersp_id": _positive_int(row.get("parentId")),
        "is_deleted": is_deleted(row),
    }


def sync_agencies(company, environment, rows, now=None):
    """Сверить справочник страховой в среде с ответом ЕРСП. Всё или ничего."""
    now = now or timezone.now()
    parsed = {}
    for row in rows:
        ersp_id, fields = agency_fields(row)
        if ersp_id in parsed:
            raise LoadError(f"агенты ЕРСП: id {ersp_id} повторяется")
        parsed[ersp_id] = (row, fields)

    result = SyncResult()
    own = ErspAgency.objects.filter(insurance_company=company, environment=environment)
    with transaction.atomic():
        existing = {obj.ersp_id: obj for obj in own.select_for_update().filter(ersp_id__in=parsed)}
        for ersp_id, (row, fields) in parsed.items():
            obj = existing.get(ersp_id)
            if obj is None:
                ErspAgency.objects.create(
                    insurance_company=company, environment=environment, ersp_id=ersp_id,
                    raw=row, synced_at=now, **fields,
                )
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
        result.hidden = own.filter(is_present=True).exclude(ersp_id__in=parsed).update(
            is_present=False, synced_at=now
        )
    return result


def load_agencies(company, user=None, client=None):
    """
    Загрузить справочник из ЕРСП (среда отправки полисов страховой).
    Возвращает (среда, итог). Ошибка ЕРСП - ErspError, ошибка в ответе -
    LoadError. Вызывать вне transaction.atomic() (как любой запрос к ЕРСП).
    """
    client = client or ErspClient.for_purpose(SEND, company, user=user)
    rows = client.references_agencies()
    return client.environment, sync_agencies(company, client.environment, rows)
