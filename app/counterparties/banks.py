"""
Банки нашего справочника и перечень банков ЦБ (references.CbuBank).

Правило (решения Бекзода 21.10): ЦБ заполняет пустое и заглушки, а то,
что ввели люди, не трогает.
- ИНН банка: пустой - заполняется из ЦБ; заполненный не меняется никогда.
  Если он отличается от ЦБ - только сообщение (возможно, введён сознательно).
- Название: заменяется названием из ЦБ, только если у нас заглушка
  «Банк <МФО>» (так банк заводится, когда провайдер ЕРСП прислал МФО без
  названия). Название, введённое или поправленное человеком, не меняется.
- Новый банк по МФО от провайдера: если МФО есть в перечне ЦБ - сразу
  с названием и ИНН из ЦБ.
- Банк, которого нет в перечне ЦБ (филиал закрыт): только сообщение,
  банк не выключается - на него ссылаются счета клиентов.
Каждое изменение по данным ЦБ - в журнал с источником «перечень банков ЦБ».
"""
from dataclasses import dataclass, field

from audit.models import Source
from references.models import CbuBank

from .models import Bank


def placeholder_name(mfo):
    """Название-заглушка банка, заведённого без названия."""
    return f"Банк {mfo}"


def cbu_record(mfo):
    """Запись перечня ЦБ по МФО (только из последнего файла) или None."""
    return CbuBank.objects.filter(mfo=mfo, is_present=True).first()


def fill_from_cbu(bank, record):
    """
    Дописать в банк пустое из записи ЦБ (без сохранения).
    Возвращает список изменённых полей: "inn", "name".
    """
    changed = []
    if not bank.inn and record.inn:
        bank.inn = record.inn
        changed.append("inn")
    if bank.name == placeholder_name(bank.mfo) and record.branch_name:
        bank.name = record.branch_name
        changed.append("name")
    return changed


def new_bank(mfo, name=""):
    """
    Новый (ещё не сохранённый) банк по МФО и источник для журнала:
    из ЦБ, если МФО есть в перечне, иначе с данным названием или заглушкой.
    """
    record = cbu_record(mfo)
    if record is not None:
        return Bank(mfo=mfo, name=record.branch_name, inn=record.inn), Source.CBU, record
    return Bank(mfo=mfo, name=name or placeholder_name(mfo)), None, None


@dataclass
class FillReport:
    """Итог сверки нашего справочника банков с перечнем ЦБ."""

    inn_filled: list = field(default_factory=list)      # МФО
    name_filled: list = field(default_factory=list)     # МФО
    inn_differs: list = field(default_factory=list)     # (МФО, наш ИНН, ИНН ЦБ)
    not_in_cbu: list = field(default_factory=list)      # МФО


def fill_all_from_cbu():
    """
    Пройти по всем нашим банкам и дописать пустое из перечня ЦБ.
    Сохраняет от имени системы с источником «перечень банков ЦБ».
    Вызывается внутри транзакции загрузки (команда load_cbu_banks).
    """
    report = FillReport()
    records = {r.mfo: r for r in CbuBank.objects.filter(is_present=True)}
    for bank in Bank.objects.order_by("mfo"):
        record = records.get(bank.mfo)
        if record is None:
            report.not_in_cbu.append(bank.mfo)
            continue
        if bank.inn and record.inn and bank.inn != record.inn:
            report.inn_differs.append((bank.mfo, bank.inn, record.inn))
        changed = fill_from_cbu(bank, record)
        if changed:
            bank.save(source=Source.CBU)
            if "inn" in changed:
                report.inn_filled.append(bank.mfo)
            if "name" in changed:
                report.name_filled.append(bank.mfo)
    return report
