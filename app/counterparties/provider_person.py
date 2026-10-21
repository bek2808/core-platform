"""
Заведение физлица по данным провайдера ЕРСП (паспорт + дата рождения или ПИНФЛ).

Два шага (как заведение юрлица по ИНН, counterparties/provider.py):
1. parse(result) - ответ провайдера -> наши значения, очищенные и проверенные;
2. create(company, data, cleaned, user) - карточка физлица + данные физлица,
   в журнал - с источником «провайдер ЕРСП».

Что отдаёт провайдер (docs/requirements/ersp-agencies.md): ПИНФЛ, документы,
ФИО латиницей, дату рождения, адрес прописки, регион и район. Типа резидентства
и страны там нет - их выбирает сотрудник на экране (по умолчанию - гражданин
Узбекистана, это видно и меняется).

Правила:
- отчество «XXX» (так провайдер отмечает его отсутствие) - пусто;
- серия и номер паспорта - из currentDocument («AD0000000»: буквы, затем цифры);
- регион берётся из ответа только если такой номер есть в нашем справочнике ЕРСП;
  иначе поле пустое и об этом - предупреждение (не угадываем);
- ответ целиком хранится в журнале обмена; сюда приходит его "result".
"""
import datetime
import re
from dataclasses import dataclass, field

from django.db import transaction

from audit.models import Source
from references.models import ErspRegion

from .models import PINFL_REGEX, Counterparty, Individual
from .normalize import remove_spaces, squash_spaces

# Так провайдер отмечает отсутствие отчества (латиницей и кириллицей).
NO_MIDDLE_NAME = {"XXX", "ХХХ"}
DOCUMENT_REGEX = re.compile(r"^([A-Z]+)([0-9]+)$")


class ProviderError(Exception):
    """Ответ провайдера нельзя применить (например, нет ПИНФЛ)."""


@dataclass
class PersonData:
    last_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    pinfl: str = ""
    passport_series: str = ""
    passport_number: str = ""
    passport_issued_by: str = ""
    passport_issue_date: datetime.date | None = None
    gender: str = ""
    birth_date: datetime.date | None = None
    address: str = ""
    region: ErspRegion | None = None
    warnings: list = field(default_factory=list)
    problems: list = field(default_factory=list)  # из-за них физлицо не завести

    @property
    def usable(self):
        return not self.problems


def _text(result, key):
    value = result.get(key)
    return squash_spaces(str(value)) if value not in (None, "") else ""


def parse(result):
    """Ответ провайдера -> PersonData: очищенное, а негодное - в problems или warnings."""
    data = PersonData()

    pinfl = remove_spaces(_text(result, "currentPinfl"))
    if not pinfl:
        pinfls = result.get("pinfls")
        if isinstance(pinfls, list) and pinfls:
            pinfl = remove_spaces(str(pinfls[0]))
    if re.fullmatch(PINFL_REGEX, pinfl):
        data.pinfl = pinfl
    else:
        # Данные берём из Узбекистана: у них ПИНФЛ есть всегда, без него ответ негоден.
        data.problems.append("У провайдера нет верного ПИНФЛ (нужно ровно 14 цифр).")

    data.last_name = _text(result, "lastNameLatin")
    data.first_name = _text(result, "firstNameLatin")
    middle = _text(result, "middleNameLatin")
    data.middle_name = "" if middle.upper() in NO_MIDDLE_NAME else middle
    if not data.last_name or not data.first_name:
        data.problems.append("У провайдера нет фамилии или имени.")

    raw_birth = _text(result, "birthDate")
    try:
        data.birth_date = datetime.date.fromisoformat(raw_birth)
    except ValueError:
        data.problems.append("У провайдера нет даты рождения (или она в непонятном виде).")

    document = remove_spaces(_text(result, "currentDocument")).upper()
    found = DOCUMENT_REGEX.match(document)
    if found:
        data.passport_series, data.passport_number = found.groups()
    elif document:
        data.warnings.append(f"Документ «{document}» не разобран на серию и номер - заполните вручную.")
    else:
        data.warnings.append("У провайдера нет действующего документа - серию и номер заполните вручную.")

    # Кем и когда выдан - из записи списка documents с тем же номером документа.
    for row in result.get("documents") or []:
        if isinstance(row, dict) and remove_spaces(str(row.get("document") or "")).upper() == document and document:
            data.passport_issued_by = squash_spaces(str(row.get("docgiveplace") or ""))
            try:
                data.passport_issue_date = datetime.date.fromisoformat(str(row.get("datebegin") or ""))
            except ValueError:
                pass
            break

    # Пол: провайдер отдаёт 1 - мужчина, 2 - женщина.
    data.gender = {"1": "m", "2": "f"}.get(_text(result, "gender"), "")

    data.address = _text(result, "address")

    region_id = result.get("regionId")
    if region_id in (None, ""):
        data.warnings.append("Провайдер не прислал регион - выберите его вручную.")
    else:
        try:
            data.region = ErspRegion.objects.filter(pk=int(region_id)).first()
        except (TypeError, ValueError):
            data.region = None
        if data.region is None:
            data.warnings.append(
                f"Регион {region_id!r} от провайдера не найден в нашем справочнике ЕРСП - "
                "выберите регион вручную (отправка в ЕРСП без региона невозможна)."
            )
    return data


def create(company, data, cleaned, user):
    """
    Новая карточка физлица по ответу провайдера. cleaned - то, что сотрудник
    подтвердил на экране: phone, address, region, resident_type, country, пол,
    серия, номер, кем и когда выдан паспорт. Остальное - из ответа.
    """
    if not data.usable:
        raise ProviderError(" ".join(data.problems))
    with transaction.atomic():
        card = Counterparty(
            insurance_company=company,
            kind=Counterparty.Kind.INDIVIDUAL,
            phone=cleaned.get("phone", ""),
            address=cleaned.get("address", ""),
            region=cleaned.get("region"),
        )
        card.full_clean()
        card.save(by=user, source=Source.PROVIDER)
        person = Individual(
            counterparty=card,
            last_name=data.last_name, first_name=data.first_name, middle_name=data.middle_name,
            pinfl=data.pinfl, birth_date=data.birth_date,
            passport_series=cleaned.get("passport_series", ""),
            passport_number=cleaned.get("passport_number", ""),
            passport_issued_by=cleaned.get("passport_issued_by", ""),
            passport_issue_date=cleaned.get("passport_issue_date"),
            gender=cleaned.get("gender", ""),
            resident_type=cleaned.get("resident_type"), country=cleaned.get("country"),
        )
        person.card_in_form = card
        person.full_clean()
        person.save(by=user, source=Source.PROVIDER)
    card.refresh_from_db()
    return card
