"""
Заполнение карточки контрагента по ИНН от провайдера ЕРСП (/api/provider/inn).

Три шага:
1. parse(result)          - ответ провайдера -> наши значения, уже очищенные
                            (пробелы, формат) и проверенные; негодное - с причиной;
2. compare(card, data)    - строки сверки «поле | у нас | у провайдера» со статусом;
3. apply(...) / create(...) - применить отмеченные строки (или завести новую
                            карточку); в журнал - с источником «провайдер ЕРСП».

Правила (решения Бекзода 20.10, docs/counterparties-model.md):
- ответ целиком хранится в журнале обмена; сюда приходит его "result";
- поля в ответе продублированы (nameShort и name_short) - берём camelCase;
- пустое у провайдера наше значение не стирает;
- значение, не прошедшее нашу проверку формата, не применяется -
  показывается причина (так ОКЭД вне справочника ЕРСП не попадёт в карточку);
- пустые у нас поля отмечены к применению заранее, отличия - нет;
- руководитель (gdFullName) не совпал с основным представителем - это другой
  человек: прежний закрывается сегодняшней датой, новый заводится основным;
- счёт, которого нет у контрагента, добавляется неосновным; основным -
  только если основного нет; новый МФО заводит банк (ИНН банка пустой).
- district (налоговая инспекция) не храним - остаётся в ответе.
"""
import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from audit.models import Source
from references.models import ErspCurrency, ErspOked

from .models import (
    LOCAL_ACCOUNT_REGEX,
    LOCAL_PHONE_REGEX,
    MFO_REGEX,
    Bank,
    BankAccount,
    Counterparty,
    LegalEntity,
    Representative,
)
from .normalize import digits_only, remove_spaces, search_key, squash_spaces

# Пары «camelCase - snake_case» в ответе провайдера: берём первое,
# расхождение со вторым показываем предупреждением.
TWINS = {
    "nameShort": "name_short",
    "gdFullName": "gd_full_name",
    "districtSoatoCode": "district_soato_code",
    "districtSoatoName": "district_soato_name",
    "bankMfo": "bank_mfo",
    "bankName": "bank_name",
    "regCertificate": "reg_number",
    "regCertificateIssueDate": "reg_date",
    "okedTitle": "oked_title",
}

# Поля карточки и данных юрлица, которые заполняет провайдер, по порядку
# показа: (ключ, где хранится, название).
FIELDS = [
    ("full_name", "legal", "полное наименование"),
    ("short_name", "legal", "краткое наименование"),
    ("address", "card", "адрес"),
    ("phone", "card", "телефон"),
    ("email", "card", "эл. почта"),
    ("oked", "legal", "ОКЭД"),
    ("okpo", "legal", "ОКПО"),
    ("soato_code", "legal", "код СОАТО района"),
    ("soato_name", "legal", "район"),
    ("reg_certificate_number", "legal", "номер свидетельства о регистрации"),
    ("reg_certificate_date", "legal", "дата свидетельства о регистрации"),
    ("charter_capital", "legal", "уставный капитал"),
]
DIRECTOR = "director"
ACCOUNT = "account"


class ProviderError(Exception):
    """Ответ провайдера нельзя применить (например, нет наименования)."""


@dataclass
class Value:
    """Значение от провайдера: очищенное, либо пустое, либо негодное с причиной."""

    value: object = None
    shown: str = ""
    problem: str = ""

    @property
    def usable(self):
        return not self.problem and self.value not in (None, "")


@dataclass
class ProviderData:
    inn: str
    values: dict = field(default_factory=dict)   # ключ из FIELDS -> Value
    director: Value = field(default_factory=Value)
    account_number: Value = field(default_factory=Value)
    bank_mfo: str = ""
    bank_name: str = ""
    warnings: list = field(default_factory=list)


# --- 1. Разбор ответа ----------------------------------------------------------------

def _text(result, key):
    value = result.get(key)
    return squash_spaces(str(value)) if value not in (None, "") else ""


def _checked(value, regex, problem):
    if not value:
        return Value()
    if not re.fullmatch(regex, value):
        return Value(shown=value, problem=problem)
    return Value(value, value)


def parse(inn, result):
    """Ответ провайдера ("result") -> ProviderData с очищенными значениями."""
    data = ProviderData(inn=inn)

    for camel, snake in TWINS.items():
        if snake in result and _text(result, camel) != _text(result, snake):
            data.warnings.append(
                f"Провайдер прислал разные значения в {camel} и {snake}; взято {camel}."
            )

    def text_value(key):
        text = _text(result, key)
        return Value(text, text) if text else Value()

    values = data.values
    values["full_name"] = text_value("name")
    values["short_name"] = text_value("nameShort")
    values["address"] = text_value("address")

    phone = digits_only(_text(result, "phone"))
    values["phone"] = _checked(phone, LOCAL_PHONE_REGEX, "Телефон не в формате 998 и 9 цифр.")

    email = _text(result, "email")
    if email:
        try:
            validate_email(email)
            values["email"] = Value(email, email)
        except ValidationError:
            values["email"] = Value(shown=email, problem="Не похоже на адрес эл. почты.")
    else:
        values["email"] = Value()

    oked = remove_spaces(_text(result, "oked"))
    if not oked:
        values["oked"] = Value()
    else:
        found = ErspOked.objects.filter(pk=oked).first()
        if found is None:
            title = _text(result, "okedTitle")
            values["oked"] = Value(
                shown=f"{oked} {title}".strip(),
                problem="Такого кода нет в справочнике ОКЭД ЕРСП - ЕРСП его не примет.",
            )
        else:
            values["oked"] = Value(found.pk, str(found))

    values["okpo"] = _checked(remove_spaces(_text(result, "okpo")), r"[0-9]{8}", "ОКПО - не 8 цифр.")
    values["soato_code"] = _checked(
        remove_spaces(_text(result, "districtSoatoCode")), r"[0-9]{7}", "Код СОАТО - не 7 цифр."
    )
    values["soato_name"] = text_value("districtSoatoName")
    values["reg_certificate_number"] = text_value("regCertificate")

    date_text = _text(result, "regCertificateIssueDate")
    if date_text:
        try:
            date = datetime.date.fromisoformat(date_text)
            values["reg_certificate_date"] = Value(date, f"{date:%d.%m.%Y}")
        except ValueError:
            values["reg_certificate_date"] = Value(shown=date_text, problem="Дата не в формате ГГГГ-ММ-ДД.")
    else:
        values["reg_certificate_date"] = Value()

    fund = result.get("fund")
    if fund in (None, ""):
        values["charter_capital"] = Value()
    else:
        try:
            amount = Decimal(str(fund)).quantize(Decimal("0.01"))
        except InvalidOperation:
            amount = None
        if amount is None or amount < 0:
            values["charter_capital"] = Value(shown=str(fund), problem="Не число или меньше нуля.")
        else:
            # Провайдер отдаёт капитал в сумах.
            values["charter_capital"] = Value((amount, ErspCurrency.UZS), money_text(amount, ErspCurrency.UZS))

    director = _text(result, "gdFullName")
    data.director = Value(director, director) if director else Value()

    data.bank_mfo = remove_spaces(_text(result, "bankMfo"))
    data.bank_name = _text(result, "bankName")
    account = remove_spaces(_text(result, "account"))
    if not account:
        data.account_number = Value()
    elif not re.fullmatch(LOCAL_ACCOUNT_REGEX, account):
        data.account_number = Value(shown=account, problem="Счёт - не 20 цифр.")
    elif not re.fullmatch(MFO_REGEX, data.bank_mfo):
        data.account_number = Value(shown=account, problem="Нет МФО банка (5 цифр) - счёт некуда привязать.")
    else:
        data.account_number = Value(account, f"{account} (МФО {data.bank_mfo})")
    return data


def money_text(amount, currency_id):
    whole = f"{amount:,.2f}".replace(",", " ")
    return f"{whole} сум" if currency_id == ErspCurrency.UZS else f"{whole} (валюта {currency_id})"


# --- 2. Сверка ----------------------------------------------------------------------------

SAME = "same"          # совпадает - применять нечего
FILL = "fill"          # у нас пусто - отмечено к применению заранее
DIFF = "diff"          # отличается - применяется только если отметить
NO_DATA = "no_data"    # у провайдера пусто - наше не трогаем
INVALID = "invalid"    # у провайдера негодное значение - не применяется


@dataclass
class Row:
    key: str
    label: str
    ours: str
    theirs: str
    status: str
    note: str = ""

    @property
    def selectable(self):
        return self.status in (FILL, DIFF)

    @property
    def preselected(self):
        return self.status == FILL


def _our_value(key, card, legal):
    if key == "charter_capital":
        if legal is None or legal.charter_capital is None:
            return None
        return (legal.charter_capital, legal.charter_capital_currency_id)
    if key == "oked":
        return legal.oked_id if legal is not None else None
    source = card if dict((k, w) for k, w, _ in FIELDS)[key] == "card" else legal
    if source is None:
        return None
    return getattr(source, key)


def _our_text(key, value, legal):
    if value in (None, ""):
        return ""
    if key == "charter_capital":
        return money_text(*value)
    if key == "oked":
        return str(legal.oked)
    if key == "reg_certificate_date":
        return f"{value:%d.%m.%Y}"
    return str(value)


def _row(key, label, ours_value, ours_text, theirs):
    if theirs.problem:
        return Row(key, label, ours_text, theirs.shown, INVALID, theirs.problem)
    if not theirs.usable:
        return Row(key, label, ours_text, "", NO_DATA)
    if ours_value in (None, ""):
        return Row(key, label, "", theirs.shown, FILL)
    if ours_value == theirs.value:
        return Row(key, label, ours_text, theirs.shown, SAME)
    return Row(key, label, ours_text, theirs.shown, DIFF)


def primary_representative(card):
    if card is None or card.pk is None:
        return None
    return card.representatives.filter(is_primary=True).first()


def active_representative_named(card, name):
    """Действующий представитель с тем же ФИО (регистр и буквы-двойники не важны)."""
    if card is None or card.pk is None:
        return None
    today = timezone.localdate()
    key = search_key(name)
    for rep in card.representatives.all():
        if search_key(rep.full_name) == key and (rep.valid_to is None or rep.valid_to >= today):
            return rep
    return None


def compare(card, data):
    """
    Строки сверки. card = None - новая карточка (у нас всё пусто).
    """
    legal = getattr(card, "legal_entity", None) if card is not None and card.pk else None
    rows = []
    for key, _where, label in FIELDS:
        ours = _our_value(key, card, legal)
        rows.append(_row(key, label, ours, _our_text(key, ours, legal), data.values[key]))

    # Руководитель -> основной представитель.
    primary = primary_representative(card)
    theirs = data.director
    if not theirs.usable:
        rows.append(Row(DIRECTOR, "руководитель (основной представитель)",
                        primary.full_name if primary else "", "", NO_DATA))
    elif primary is None:
        rows.append(Row(DIRECTOR, "руководитель (основной представитель)", "", theirs.shown, FILL,
                        "Будет заведён основным представителем (без должности и паспорта)."))
    elif search_key(primary.full_name) == search_key(theirs.value):
        rows.append(Row(DIRECTOR, "руководитель (основной представитель)",
                        primary.full_name, theirs.shown, SAME))
    else:
        existing = active_representative_named(card, theirs.value)
        note = (
            f"Он уже есть среди представителей - станет основным; {primary.full_name} "
            f"останется представителем, но не основным."
            if existing else
            f"Другой человек: {primary.full_name} - срок полномочий закроется "
            f"сегодняшней датой, {theirs.value} - заведётся основным."
        )
        rows.append(Row(DIRECTOR, "руководитель (основной представитель)",
                        primary.full_name, theirs.shown, DIFF, note))

    # Расчётный счёт.
    label = "расчётный счёт"
    account = data.account_number
    our_primary = None
    if card is not None and card.pk:
        our_primary = card.accounts.filter(is_primary=True).select_related("bank").first()
    ours_text = ""
    if our_primary is not None:
        bank = f"МФО {our_primary.bank.mfo}" if our_primary.bank_id else our_primary.foreign_bank_name
        ours_text = f"{our_primary.number} ({bank})"
    if account.problem:
        rows.append(Row(ACCOUNT, label, ours_text, account.shown, INVALID, account.problem))
    elif not account.usable:
        rows.append(Row(ACCOUNT, label, ours_text, "", NO_DATA))
    else:
        bank_note = ""
        if not Bank.objects.filter(mfo=data.bank_mfo).exists():
            bank_note = f" Банка с МФО {data.bank_mfo} в справочнике нет - будет заведён (ИНН банка - дописать вручную)."
        exists = card is not None and card.pk and card.accounts.filter(number=account.value).first()
        if exists:
            state = "основной" if exists.is_primary else ("закрыт" if exists.is_closed else "неосновной")
            rows.append(Row(ACCOUNT, label, ours_text, account.shown, SAME, f"Такой счёт уже есть ({state})."))
        elif our_primary is None:
            rows.append(Row(ACCOUNT, label, "", account.shown, FILL,
                            "Будет добавлен основным счётом." + bank_note))
        else:
            rows.append(Row(ACCOUNT, label, ours_text, account.shown, DIFF,
                            "Будет добавлен неосновным: основной счёт остаётся прежним." + bank_note))
    return rows


# --- 3. Применение --------------------------------------------------------------------

def _set_field(obj, key, value):
    if key == "charter_capital":
        obj.charter_capital, obj.charter_capital_currency_id = value
    elif key == "oked":
        obj.oked_id = value
    else:
        setattr(obj, key, value)


def _save(obj, user, update_fields=None):
    """Проверить и сохранить запись с источником «провайдер ЕРСП»."""
    obj.full_clean()
    obj.save(by=user, source=Source.PROVIDER, update_fields=update_fields)


def _db_fields(keys):
    names = []
    for key in keys:
        names += ["charter_capital", "charter_capital_currency"] if key == "charter_capital" else [key]
    return names


def apply(card, data, selected, user):
    """
    Применить к карточке выбранные строки сверки. selected - ключи строк,
    отмеченные сотрудником; применяются только те, что можно (FILL / DIFF).
    Возвращает список того, что сделано (для сообщения сотруднику).
    """
    rows = {row.key: row for row in compare(card, data)}
    chosen = [key for key in selected if key in rows and rows[key].selectable]
    done = []
    with transaction.atomic():
        legal = card.legal_entity
        where = {key: w for key, w, _ in FIELDS}
        card_keys = [k for k in chosen if where.get(k) == "card"]
        legal_keys = [k for k in chosen if where.get(k) == "legal"]
        for key in card_keys:
            _set_field(card, key, data.values[key].value)
        if card_keys:
            _save(card, user, update_fields=_db_fields(card_keys))
        for key in legal_keys:
            _set_field(legal, key, data.values[key].value)
        if legal_keys:
            legal.card_in_form = card
            _save(legal, user, update_fields=_db_fields(legal_keys))
        done += [rows[k].label for k in card_keys + legal_keys]

        if DIRECTOR in chosen:
            done.append(_apply_director(card, data.director.value, user))
        if ACCOUNT in chosen:
            done.append(_apply_account(card, data, user))
        card.fill_region_from_soato(by=user)
    return done


def _apply_director(card, name, user):
    primary = primary_representative(card)
    existing = active_representative_named(card, name)
    if primary is not None and existing is not None and existing.pk == primary.pk:
        return "руководитель: без изменений"
    if primary is not None:
        primary.is_primary = False
        if existing is None:
            # Другой человек стал руководителем: полномочия прежнего закрываются.
            today = timezone.localdate()
            primary.valid_to = max(today, primary.valid_from) if primary.valid_from else today
        _save(primary, user)
    if existing is not None:
        existing.is_primary = True
        _save(existing, user)
        return f"основной представитель: {existing.full_name}"
    new = Representative(counterparty=card, full_name=name, is_primary=True)
    _save(new, user)
    if primary is not None:
        return f"руководитель: {primary.full_name} закрыт сегодняшней датой, заведён {name}"
    return f"заведён основной представитель: {name}"


def _apply_account(card, data, user):
    bank = Bank.objects.filter(mfo=data.bank_mfo).first()
    note = ""
    if bank is None:
        bank = Bank(mfo=data.bank_mfo, name=data.bank_name or f"Банк {data.bank_mfo}")
        _save(bank, user)
        note = f"; заведён банк {bank.mfo} - допишите его ИНН в справочнике банков"
    has_primary = card.accounts.filter(is_primary=True).exists()
    account = BankAccount(
        counterparty=card, bank=bank, number=data.account_number.value, is_primary=not has_primary
    )
    _save(account, user)
    return f"счёт {account.number} добавлен {'основным' if account.is_primary else 'неосновным'}{note}"


def create(company, data, selected, user):
    """
    Новая карточка юрлица по ответу провайдера. Применяются выбранные строки
    сверки (у новой карточки все годные строки - «заполнить»).
    """
    if not data.values["full_name"].usable:
        raise ProviderError("У провайдера нет наименования организации - карточку не завести.")
    with transaction.atomic():
        card = Counterparty(insurance_company=company)
        _save(card, user)
        legal = LegalEntity(counterparty=card, inn=data.inn, full_name=data.values["full_name"].value)
        _save(legal, user)
        card.refresh_from_db()
        apply(card, data, [k for k in selected if k != "full_name"], user)
    return card
