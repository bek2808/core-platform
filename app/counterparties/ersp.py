"""
Готовность контрагента к отправке в ЕРСП: чего не хватает.

Карточку можно сохранять неполной; полнота проверяется здесь - перед
отправкой и в карточке (блок «Готовность к ЕРСП»). Правила - из требований
Бекзода (docs/requirements/counterparties-legal-entities.md) и модели
(docs/counterparties-model.md, «Правила отправки в ЕРСП»).

Что берётся для ЕРСП:
- представитель - ОСНОВНОЙ, с действующим сроком;
- счёт - ОСНОВНОЙ (закрытый основным быть не может).
Если основного нет - это пробел, «первый попавшийся» не берётся.

Иностранной организации не нужны регион и ОКЭД: при отправке система
ставит регион нашей страховой компании и ОКЭД 0 (настоящий - в comment).

Роль в договоре: форма собственности обязательна только страхователю
(решение Бекзода 20.10); у остальных участников её можно отправить,
но без неё они готовы. Обязательность для страхователя проверить
в песочнице ЕРСП при тестировании продаж (например, ДМС).
"""
from django.db import models
from django.utils import timezone


class Role(models.TextChoices):
    INSURANT = "insurant", "страхователь"
    OTHER = "other", "выгодоприобретатель, застрахованный, владелец объекта"


def ersp_gaps(counterparty, role=Role.INSURANT):
    """
    Список того, чего не хватает для отправки в ЕРСП в этой роли.
    Пустой список - контрагент готов.
    """
    legal = getattr(counterparty, "legal_entity", None)
    if legal is None:
        return ["Нет данных юрлица."]

    gaps = []
    local = not legal.is_foreign

    # --- Карточка и данные юрлица -------------------------------------------------
    if not legal.inn:
        gaps.append("Данные юрлица: нет ИНН.")
    if not legal.full_name:
        gaps.append("Данные юрлица: нет полного наименования.")
    if not counterparty.address:
        gaps.append("Карточка: нет адреса.")
    if local:
        if counterparty.region_id is None:
            gaps.append("Карточка: нет региона.")
        if legal.oked_id is None:
            gaps.append("Данные юрлица: нет ОКЭД.")
        if not counterparty.phone:
            gaps.append("Карточка: нет телефона (местной организации обязателен).")
    elif not counterparty.email:
        gaps.append("Карточка: нет эл. почты (иностранной организации обязательна).")
    if role == Role.INSURANT and legal.ownership_form_id is None:
        gaps.append("Данные юрлица: нет формы собственности (обязательна страхователю).")

    # --- Основной представитель -----------------------------------------------------
    representative = counterparty.representatives.filter(is_primary=True).first()
    if representative is None:
        gaps.append("Представитель: нет основного представителя.")
    else:
        if representative.valid_to and representative.valid_to < timezone.localdate():
            gaps.append(
                f"Представитель: срок полномочий основного представителя истёк "
                f"{representative.valid_to:%d.%m.%Y}."
            )
        if local:
            if not representative.power_basis_text:
                gaps.append("Представитель: нет основания полномочий.")
            if not representative.power_basis_number:
                gaps.append("Представитель: нет номера документа-основания.")
            if not representative.pinfl:
                gaps.append("Представитель: нет ПИНФЛ.")
            if not representative.passport_series or not representative.passport_number:
                gaps.append("Представитель: нет серии или номера паспорта.")

    # --- Основной счёт -------------------------------------------------------------
    account = counterparty.accounts.filter(is_primary=True, is_closed=False).first()
    if account is None:
        gaps.append("Счёт: нет основного счёта.")
    elif local:
        if account.bank_id is None:
            gaps.append(
                "Счёт: банк основного счёта не из справочника - для местной "
                "организации нужны МФО и ИНН банка."
            )
        elif not account.bank.inn:
            gaps.append(f"Счёт: у банка {account.bank.mfo} не заполнен ИНН (справочник банков).")

    return gaps
