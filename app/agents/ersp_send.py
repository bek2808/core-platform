"""
Отправка агента в ЕРСП (часть B5; требования - docs/requirements/ersp-agencies.md).

Решения Бекзода 21-22.10 (docs/users-model.md, решения 31, 41-47):
- отправка - только в ПЕСОЧНИЦУ: из боевой среды агента потом не удалить;
- метод update очищает непереданные необязательные поля, поэтому пакет
  всегда собирается целиком из нашей карточки (тест на это обязателен);
- «добавить» или «изменить» выбирается по тому, есть ли у агента действующая
  связь с ЕРСП в песочнице (agencyId);
- без скана соглашения отправка не начинается: система называет, чего не хватает;
- перед отправкой - «Проверка»: тот же запрос собирается и показывается
  как есть, проверяются правила ЕРСП, справочник песочницы обновляется
  (чтение) и сверяется. В ЕРСП при проверке ничего не записывается.

Три шага:
1. build(agent)            - пакет запроса и список «чего не хватает» (без сети);
2. check(agent, user)      - build + обновление справочника из ЕРСП + сверка;
3. send(agent, user, ...)  - настоящая отправка и связь с agencyId.

Поля запроса и правила - из присланного описания методов /api/agencies/add и
/api/agencies/update; названия вложенных полей - `agency[person][fullName][firstname]`
(как в Swagger). Что ЕРСП вернёт в "result", станет ясно после первой отправки:
agencyId ищется в ответе, а если его там нет - в обновлённом справочнике.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field

from django.utils import timezone

from companies.models import Department
from counterparties.models import LOCAL_PHONE_REGEX, Counterparty
from files import storage
from integrations.agencies import sync_agencies
from integrations.ersp import ErspClient, ErspError
from integrations.models import Environment, ErspAgency
from references.models import ErspResidentType

from .models import ErspLink, agency_id_for_department

# Только песочница (решение 31).
# Принято на практике 22.10: 0,69 МБ прошёл, 1,38 МБ - 413 от nginx. Берём с запасом на остальные поля.
MAX_FILE_BYTES = 1_000_000
ENVIRONMENT = Environment.SANDBOX
FILE_FIELD = "agencyAgreementFile"
NO_MIDDLE_NAME = "ХХХ"  # кириллические «Х»: так требует ЕРСП («при отсутствии указать ХХХ»)
SECRET_FIELDS = {"electronAgencyApiToken"}


class SendRefused(Exception):
    """Отправка не выполнена по правилам (текст - для сотрудника)."""


@dataclass
class AgencyRequest:
    agent: object
    operation: str = "add"            # add | update
    ersp_id: int | None = None        # agencyId при update
    fields: list = field(default_factory=list)   # [(ключ, значение-строка)]
    file: tuple | None = None         # (поле, имя файла, содержимое, тип)
    gaps: list = field(default_factory=list)     # чего не хватает - отправка невозможна
    parent_ersp_id: int | None = None
    # Для сверки со справочником: по чему искать дубль.
    inn: str = ""
    pinfl: str = ""

    @property
    def path(self):
        return "/api/agencies/add" if self.operation == "add" else "/api/agencies/update"

    @property
    def ready(self):
        return not self.gaps

    @property
    def method_label(self):
        return "добавить агента (add)" if self.operation == "add" else f"изменить агента (update, agencyId {self.ersp_id})"

    def shown_fields(self):
        """Поля для показа на экране: секреты скрыты."""
        return [(key, "••••" if key.rsplit("[", 1)[-1].rstrip("]") in SECRET_FIELDS else value)
                for key, value in self.fields]

    def file_info(self):
        if self.file is None:
            return None
        _, name, content, mime = self.file
        return {"name": name, "size": len(content), "type": mime, "sha256": hashlib.sha256(content).hexdigest()}

    def fingerprint(self):
        """Отпечаток запроса: проверенное и отправленное совпадают, пока он тот же."""
        material = json.dumps([self.operation, self.ersp_id, self.fields, self.file_info()], ensure_ascii=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _text(value):
    return "" if value is None else str(value).strip()


def _flag(value):
    """ЕРСП (Laravel) принимает в форме 1 / 0; строки «true» / «false» - ошибка 422 (проверено 22.10)."""
    return "1" if value else "0"


def _date(value):
    return value.isoformat() if value else ""


def _need(gaps, value, text):
    if value in (None, ""):
        gaps.append(text)
        return False
    return True


def build(agent):
    """Пакет запроса для агента и список того, чего не хватает (без сети)."""
    request = AgencyRequest(agent=agent)
    gaps = request.gaps
    card = agent.counterparty
    fields = request.fields

    # --- Что уже есть в ЕРСП: добавить или изменить -----------------------------------
    link = ErspLink.objects.filter(
        agent=agent, environment=ENVIRONMENT, is_active=True,
    ).select_related("ersp_agency").first()
    if link is not None:
        if not link.ersp_agency.can_be_linked():
            gaps.append(
                f"Запись {link.ersp_agency.ersp_id} удалена в ЕРСП или пропала из справочника: "
                "снимите связь с ЕРСП в карточке агента, тогда агент отправится как новый."
            )
        request.operation = "update"
        request.ersp_id = link.ersp_agency.ersp_id

    # --- Агент ---------------------------------------------------------------------------
    if agent.is_electronic or agent.is_head_electronic:
        gaps.append(
            "Электронные агенты отсюда пока не отправляются (нужны виды страхования, адрес API "
            "и токен) - заведите такого агента в ЕРСП вручную."
        )
    _need(gaps, agent.area_type_id, "Карточка агента: нет типа местности.")
    _need(gaps, agent.formation_date, "Карточка агента: нет даты образования.")
    prefix = "Broker" if agent.kind == agent.Kind.BROKER else "Агент"
    name = f"{prefix}: {card.display_name}" if card.display_name else ""
    _need(gaps, name, "У контрагента нет наименования.")

    # --- Соглашение и скан ---------------------------------------------------------------
    agreement = agent.current_agreement()
    if agreement is None:
        gaps.append("Нет действующего соглашения с агентом.")
    else:
        if agreement.scan_id is None:
            gaps.append(
                f"У соглашения {agreement.number} нет скана: загрузите PDF в блоке «Соглашения». "
                "Без скана отправка не начинается."
            )
        else:
            scan = agreement.scan
            try:
                content = storage.open_path(scan).read_bytes()
            except FileNotFoundError:
                gaps.append(
                    f"Скан соглашения {agreement.number} записан в базе, но файла нет на диске: "
                    "сообщите администратору (команда files_check)."
                )
            else:
                if len(content) > MAX_FILE_BYTES:
                    gaps.append(
                        f"Скан соглашения {agreement.number} слишком большой: {len(content) / 1_000_000:.2f} МБ. "
                        "Сервер ЕРСП не принимает запросы больше примерно 1 МБ (413) - сожмите PDF "
                        "и загрузите скан заново («Заменить скан»)."
                    )
                request.file = (FILE_FIELD, scan.original_name, content, scan.content_type)

    # --- Куда привязан -------------------------------------------------------------------
    placement = agent.current_placement()
    if placement is None:
        gaps.append("Агент не прикреплён к подразделению: родитель в ЕРСП (parentId) неизвестен.")
    else:
        parent = agency_id_for_department(placement.department, ENVIRONMENT)
        if parent is None:
            place = placement.department
            if place.kind == Department.Kind.DIVISION:
                place = place.parent
            gaps.append(
                f"У «{place.full_name()}» нет живой связи с ЕРСП в песочнице: "
                "родитель агента (parentId) неизвестен."
            )
        request.parent_ersp_id = parent

    # --- Данные физлица или организации ----------------------------------------------------
    if card.kind == Counterparty.Kind.INDIVIDUAL:
        body = _person_fields(agent, card, gaps, request)
    else:
        body = _organization_fields(agent, card, gaps, request)

    # Поля собираются и при пробелах: на экране видно, что уйдёт, когда всё будет заполнено.
    if request.operation == "update":
        fields.append(("id", str(request.ersp_id)))
    fields.append(("name", name))
    fields.append(("parentId", _text(request.parent_ersp_id)))
    fields.append(("agencyType", "2"))  # агент (1 - филиал)
    fields.append(("areaTypeId", _text(agent.area_type_id)))
    if agreement is not None:
        fields.append(("agentAgreementNumber", agreement.number))
        fields.append(("agentAgreementDate", _date(agreement.signed_on)))
    fields.append(("formationDate", _date(agent.formation_date)))
    fields.append(("isElectronAgency", _flag(agent.is_electronic)))
    if agent.is_electronic:
        # Для обычного агента это поле нельзя отправлять вовсе (422 «prohibited unless is electron agency is true»).
        fields.append(("isHeadElectronAgency", _flag(agent.is_head_electronic)))
    fields.extend(body)
    return request


def _phone_ok(gaps, phone, who):
    if not phone:
        gaps.append(f"{who}: нет телефона.")
    elif not re.fullmatch(LOCAL_PHONE_REGEX, phone):
        gaps.append(f"{who}: телефон «{phone}» - нужен формат 998 и 9 цифр.")


def _person_fields(agent, card, gaps, request):
    person = getattr(card, "individual", None)
    if person is None:
        gaps.append("У физлица нет данных (ФИО, ПИНФЛ).")
        return []
    who = "Физлицо"
    _need(gaps, card.region_id, f"{who}: нет региона.")
    _need(gaps, card.address, f"{who}: нет адреса.")
    _phone_ok(gaps, card.phone, who)
    _need(gaps, person.birth_date, f"{who}: нет даты рождения.")
    _need(gaps, person.resident_type_id, f"{who}: не указан тип резидентства.")
    _need(gaps, person.country_id, f"{who}: не указана страна.")
    _need(gaps, person.passport_series, f"{who}: нет серии паспорта.")
    _need(gaps, person.passport_number, f"{who}: нет номера паспорта.")
    if person.resident_type_id == ErspResidentType.CITIZEN:
        _need(gaps, person.pinfl, f"{who}: нет ПИНФЛ (у гражданина обязателен).")
    request.pinfl = person.pinfl
    base = "agency[person]"
    return [
        (f"{base}[regionId]", _text(card.region_id)),
        (f"{base}[birthDate]", _date(person.birth_date)),
        (f"{base}[address]", card.address),
        (f"{base}[residentType]", _text(person.resident_type_id)),
        (f"{base}[countryId]", _text(person.country_id)),
        (f"{base}[phone]", card.phone),
        (f"{base}[passportData][pinfl]", person.pinfl),
        (f"{base}[passportData][seria]", person.passport_series),
        (f"{base}[passportData][number]", person.passport_number),
        (f"{base}[fullName][firstname]", person.first_name),
        (f"{base}[fullName][lastname]", person.last_name),
        (f"{base}[fullName][middlename]", person.middle_name or NO_MIDDLE_NAME),
    ]


def _organization_fields(agent, card, gaps, request):
    legal = getattr(card, "legal_entity", None)
    if legal is None:
        gaps.append("У организации нет данных юрлица.")
        return []
    who = "Организация"
    if legal.is_foreign:
        gaps.append(f"{who}: иностранная - в ЕРСП агентами заводятся местные организации.")
    _need(gaps, legal.inn, f"{who}: нет ИНН.")
    _need(gaps, card.region_id, f"{who}: нет региона.")
    _need(gaps, card.address, f"{who}: нет адреса.")
    _need(gaps, legal.oked_id, f"{who}: нет ОКЭД.")
    _phone_ok(gaps, card.phone, who)
    account = agent.commission_account
    if account is None:
        gaps.append("Карточка агента: нет счёта для комиссии (в ЕРСП это расчётный счёт, обязателен).")
    representative = card.representatives.filter(is_primary=True).first()
    today = timezone.localdate()
    if representative is None:
        gaps.append(f"{who}: нет основного представителя.")
    elif representative.valid_to and representative.valid_to < today:
        gaps.append(f"{who}: срок полномочий основного представителя истёк.")
    else:
        _need(gaps, representative.position_text, f"{who}: у основного представителя нет должности.")
    request.inn = legal.inn
    base = "agency[organization]"
    fields = [
        (f"{base}[inn]", legal.inn),
        (f"{base}[name]", legal.full_name),
        (f"{base}[representativeName]", representative.full_name if representative else ""),
        (f"{base}[address]", card.address),
        (f"{base}[position]", representative.position_text if representative else ""),
        (f"{base}[checkingAccount]", account.number if account else ""),
        (f"{base}[phone]", card.phone),
        (f"{base}[regionId]", _text(card.region_id)),
        (f"{base}[oked]", _text(legal.oked_id)),
    ]
    if legal.ownership_form_id:
        fields.append((f"{base}[ownershipFormId]", _text(legal.ownership_form_id)))
    return fields


# --- Проверка перед отправкой ----------------------------------------------------------


@dataclass
class Check:
    request: AgencyRequest
    problems: list = field(default_factory=list)   # мешают отправке
    notes: list = field(default_factory=list)      # к сведению
    checked_at: object = None
    directory: object = None                       # итог загрузки справочника

    @property
    def can_send(self):
        return self.request.ready and not self.problems


def compare_with_directory(request):
    """
    Сверка с копией справочника ЕРСП песочницы (ErspAgency): родитель есть и не
    удалён; агента с таким ИНН / ПИНФЛ у страховой ещё нет (при добавлении ЕРСП
    такого не примет). Возвращает (мешающее, к сведению).
    """
    agent = request.agent
    company = agent.insurance_company
    rows = ErspAgency.objects.filter(insurance_company=company, environment=ENVIRONMENT)
    problems, notes = [], []

    if request.parent_ersp_id is not None:
        parent = rows.filter(ersp_id=request.parent_ersp_id).first()
        if parent is None or not parent.can_be_linked():
            problems.append(
                f"Родитель {request.parent_ersp_id} не найден в справочнике ЕРСП (песочница) "
                "или удалён там."
            )
        else:
            notes.append(f"Родитель: {parent.ersp_id} - {parent.name}.")

    twins = ErspAgency.objects.none()
    if request.inn:
        twins = rows.filter(inn=request.inn)
    elif request.pinfl:
        twins = rows.filter(pinfl=request.pinfl)
    twins = twins.filter(is_deleted=False, is_present=True)
    if request.operation == "update":
        twins = twins.exclude(ersp_id=request.ersp_id)
    for twin in twins:
        problems.append(
            f"В ЕРСП (песочница) уже есть агент с таким {'ИНН' if request.inn else 'ПИНФЛ'}: "
            f"{twin.ersp_id} - {twin.name}. ЕРСП не принимает второго у одной страховой: "
            "свяжите существующую запись с агентом в блоке «Связь с ЕРСП»."
        )
    if request.operation == "add" and not twins:
        notes.append("Такого ИНН / ПИНФЛ у страховой в песочнице ещё нет - дубля не будет.")
    return problems, notes


def check(agent, user):
    """
    «Проверка перед отправкой»: пакет, правила, обновление справочника из ЕРСП
    (только чтение) и сверка. Бросает ErspError, если справочник не загрузился.
    Вызывать вне transaction.atomic() (запись журнала обмена идёт отдельно).
    """
    request = build(agent)
    result = Check(request=request, checked_at=timezone.now())
    client = ErspClient(ENVIRONMENT, agent.insurance_company, user=user)
    rows = client.references_agencies()
    result.directory = sync_agencies(agent.insurance_company, ENVIRONMENT, rows)
    result.problems, result.notes = compare_with_directory(request)
    return result


# --- Отправка ----------------------------------------------------------------------------


@dataclass
class SendResult:
    operation: str
    exchange: object
    ersp_id: int | None = None
    linked: bool = False
    message: str = ""


def _id_from_answer(answer):
    """agencyId из ответа ЕРСП: сам ответ - число, либо поле id / agencyId."""
    if isinstance(answer, bool):
        return None
    if isinstance(answer, int):
        return answer if answer > 0 else None
    if isinstance(answer, dict):
        for key in ("id", "agencyId"):
            value = answer.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
    return None


def send(agent, user, fingerprint):
    """
    Настоящая отправка в песочницу. fingerprint - отпечаток запроса, который
    сотрудник видел при проверке: данные изменились - отправки нет.
    Вызывать вне transaction.atomic().
    """
    request = build(agent)
    if not request.ready:
        raise SendRefused("Не хватает данных: " + " ".join(request.gaps))
    if fingerprint != request.fingerprint():
        raise SendRefused("Данные агента изменились с момента проверки. Проверьте заново.")
    problems, _ = compare_with_directory(request)
    if problems:
        raise SendRefused(" ".join(problems))

    client = ErspClient(ENVIRONMENT, agent.insurance_company, user=user)
    writer = client.agencies_add if request.operation == "add" else client.agencies_update
    answer = writer(request.fields, request.file)  # ErspError - наверх, запись в журнале обмена есть
    outcome = SendResult(operation=request.operation, exchange=client.last_exchange)

    # Что теперь в ЕРСП: обновляем справочник и ищем запись агента.
    try:
        rows = client.references_agencies()
    except ErspError as error:
        outcome.message = (
            f"Агент отправлен, но справочник обновить не удалось ({error.message}). "
            "Загрузите справочник и свяжите агента в блоке «Связь с ЕРСП»."
        )
        return outcome
    sync_agencies(agent.insurance_company, ENVIRONMENT, rows)

    if request.operation == "update":
        outcome.ersp_id = request.ersp_id
        outcome.message = f"Агент изменён в ЕРСП (песочница), agencyId {request.ersp_id}."
        return outcome

    found = _find_new_record(agent, request, answer)
    if found is None:
        outcome.message = (
            "Агент отправлен, но его agencyId определить не удалось (в ответе ЕРСП его нет, "
            "в справочнике одинаковой записи не нашлось). Свяжите агента в блоке «Связь с ЕРСП» "
            "после проверки справочника."
        )
        return outcome
    link = ErspLink(ersp_agency=found, agent=agent)
    link.full_clean()
    link.save(by=user)
    outcome.ersp_id = found.ersp_id
    outcome.linked = True
    outcome.message = f"Агент добавлен в ЕРСП (песочница), agencyId {found.ersp_id}; связь записана."
    return outcome


def _find_new_record(agent, request, answer):
    """Только что добавленная запись: по id из ответа, иначе по ИНН / ПИНФЛ среди несвязанных."""
    rows = ErspAgency.objects.filter(
        insurance_company=agent.insurance_company, environment=ENVIRONMENT, is_deleted=False, is_present=True,
    )
    answered = _id_from_answer(answer)
    if answered is not None:
        found = rows.filter(ersp_id=answered).first()
        if found is not None:
            return found
    key = {"inn": request.inn} if request.inn else {"pinfl": request.pinfl} if request.pinfl else None
    if key is None:
        return None
    candidates = rows.filter(**key, link__isnull=True)
    return candidates.first() if candidates.count() == 1 else None
