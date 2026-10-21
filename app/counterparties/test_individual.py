"""
Автотесты физлица: данные физлица (counterparties.Individual), разбор ответа
провайдера по паспорту / ПИНФЛ и экран «Новое физлицо» в админ-панели.

Что проверяется:
- разбор ответа (отчество «XXX», серия и номер из документа, регион из справочника,
  негодный ответ - причина, а не молчание);
- проверки в базе даже в обход формы: ПИНФЛ, повтор в страховой, страховая
  как у карточки, данные физлица - только у карточки физлица;
- экран: запрос только по кнопке, повтор страницы не шлёт запрос заново,
  уже заведённый ПИНФЛ ведёт на карточку, права и чужая страховая;
- журнал изменений: источник «провайдер ЕРСП».

В сеть тесты не ходят: ЕРСП заменён заглушкой FakeErsp (integrations/tests.py).

Запуск на сервере: bin/test counterparties.test_individual
"""
import copy
import datetime

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from accounts.models import User
from accounts.testing import give_roles
from audit.models import ChangeLog, Source
from config.testing import refusal_expected
from integrations import ersp
from integrations.models import Exchange
from integrations.tests import ErspMixin, FakeErsp, token_answer
from references.models import ErspCountry, ErspRegion, ErspResidentType

from . import provider_person
from .models import Counterparty, Individual
from .tests import ADMIN_GROUP, EMPLOYEE_GROUP, STRONG_PASSWORD, CounterpartyTestCase

SENDER_PINFL = "30202020202020"
PINFL = "30101990000000"
LIST_URL = "/admin/counterparties/counterparty/"
URL = f"{LIST_URL}by-passport/"

# Ответ провайдера (значения условные; форма - из docs/requirements/ersp-agencies.md).
PERSON_RESULT = {
    "currentPinfl": PINFL,
    "pinfls": [PINFL],
    "currentDocument": "AD0000000",
    "documents": [
        {"document": "AD0000000", "type": "IDMS_RECV_MVD_IDCARD_CITIZEN", "status": 2,
         "docgiveplace": "IIB Test", "datebegin": "2022-08-30", "dateend": "2032-08-29"},
        {"document": "AA0000000", "type": "IDMS_RECV_CITIZ_DOCUMENTS", "status": 5,
         "docgiveplace": "Old", "datebegin": "2012-08-29", "dateend": "2022-08-28"},
    ],
    "lastNameLatin": "FAMILIYA",
    "firstNameLatin": "ISM",
    "middleNameLatin": "OTCHESTVO",
    "birthDate": "1990-01-01",
    "gender": "1",
    "address": "Toshkent sh., Test MFY, 1-uy  ",
    "regionId": 10,
    "districtId": 1001,
}


def person(**changes):
    result = copy.deepcopy(PERSON_RESULT)
    result.update(changes)
    return result


def confirmed(**changes):
    """Что сотрудник подтвердил на втором шаге."""
    data = {
        "passport_series": "AD", "passport_number": "0000000", "phone": "998901234567",
        "address": "Toshkent sh., Test MFY, 1-uy", "region": ErspRegion.objects.get(pk=10),
        "passport_issued_by": "IIB Test", "passport_issue_date": datetime.date(2022, 8, 30), "gender": "m",
        "resident_type": ErspResidentType.objects.get(pk=ErspResidentType.CITIZEN),
        "country": ErspCountry.objects.get(pk=ErspCountry.UZBEKISTAN),
    }
    data.update(changes)
    return data


class IndividualCase(CounterpartyTestCase):
    def make_person(self, company=None, pinfl=PINFL, by=None):
        card = Counterparty(insurance_company=company or self.mosaic, kind=Counterparty.Kind.INDIVIDUAL)
        card.full_clean()
        card.save(by=by)
        individual = Individual(
            counterparty=card, last_name="FAMILIYA", first_name="ISM", pinfl=pinfl,
            birth_date=datetime.date(1990, 1, 1),
        )
        individual.full_clean()
        individual.save(by=by)
        card.refresh_from_db()
        return card, individual


class ParseTests(IndividualCase):
    def test_full_answer(self):
        data = provider_person.parse(person())
        self.assertTrue(data.usable)
        self.assertEqual((data.last_name, data.first_name, data.middle_name), ("FAMILIYA", "ISM", "OTCHESTVO"))
        self.assertEqual(data.pinfl, PINFL)
        self.assertEqual((data.passport_series, data.passport_number), ("AD", "0000000"))
        self.assertEqual(data.birth_date, datetime.date(1990, 1, 1))
        self.assertEqual(data.address, "Toshkent sh., Test MFY, 1-uy")  # лишние пробелы убраны
        self.assertEqual(data.region.pk, 10)
        self.assertEqual(data.gender, "m")
        # Кем и когда выдан - у действующего документа, не у старого.
        self.assertEqual((data.passport_issued_by, data.passport_issue_date), ("IIB Test", datetime.date(2022, 8, 30)))
        self.assertEqual(data.warnings, [])

    def test_gender_female_and_unknown(self):
        self.assertEqual(provider_person.parse(person(gender="2")).gender, "f")
        self.assertEqual(provider_person.parse(person(gender="9")).gender, "")
        self.assertEqual(provider_person.parse(person(gender=None)).gender, "")

    def test_no_middle_name_marker_is_empty(self):
        for marker in ("XXX", "xxx", "ХХХ"):  # латиница и кириллица
            self.assertEqual(provider_person.parse(person(middleNameLatin=marker)).middle_name, "")

    def test_region_unknown_to_us_is_not_guessed(self):
        data = provider_person.parse(person(regionId=999999))
        self.assertIsNone(data.region)
        self.assertTrue(data.usable)  # завести можно, регион выберет сотрудник
        self.assertTrue(any("не найден в нашем справочнике" in w for w in data.warnings))

    def test_no_region_warns(self):
        data = provider_person.parse(person(regionId=None))
        self.assertIsNone(data.region)
        self.assertTrue(any("не прислал регион" in w for w in data.warnings))

    def test_document_not_split_warns(self):
        data = provider_person.parse(person(currentDocument="ПАСПОРТ-1"))
        self.assertEqual((data.passport_series, data.passport_number), ("", ""))
        self.assertTrue(any("не разобран" in w for w in data.warnings))
        self.assertTrue(data.usable)

    def test_pinfl_taken_from_list_when_no_current(self):
        data = provider_person.parse(person(currentPinfl=None))
        self.assertEqual(data.pinfl, PINFL)

    def test_bad_answer_has_problems(self):
        for changes, text in [
            ({"currentPinfl": "123", "pinfls": []}, "ПИНФЛ"),
            ({"lastNameLatin": ""}, "фамилии или имени"),
            ({"firstNameLatin": None}, "фамилии или имени"),
            ({"birthDate": "не дата"}, "даты рождения"),
            ({"birthDate": None}, "даты рождения"),
        ]:
            data = provider_person.parse(person(**changes))
            self.assertFalse(data.usable, changes)
            self.assertIn(text, " ".join(data.problems), changes)

    def test_create_refuses_bad_answer(self):
        data = provider_person.parse(person(currentPinfl="1", pinfls=[]))
        with self.assertRaises(provider_person.ProviderError):
            provider_person.create(self.mosaic, data, confirmed(), self.bekzod)
        self.assertFalse(Counterparty.objects.exists())


class CreateTests(IndividualCase):
    def test_create_individual(self):
        card = provider_person.create(self.mosaic, provider_person.parse(person()), confirmed(), self.bekzod)
        self.assertEqual(card.kind, Counterparty.Kind.INDIVIDUAL)
        self.assertEqual(card.display_name, "FAMILIYA ISM OTCHESTVO")
        self.assertIn(PINFL, card.search_text)
        self.assertEqual((card.phone, card.region_id), ("998901234567", 10))
        individual = card.individual
        self.assertEqual(individual.insurance_company, self.mosaic)
        self.assertEqual(individual.passport, "AD0000000")
        self.assertEqual((individual.gender, individual.passport_issued_by), ("m", "IIB Test"))
        self.assertEqual(individual.passport_issue_date, datetime.date(2022, 8, 30))
        self.assertEqual(individual.resident_type_id, ErspResidentType.CITIZEN)
        self.assertEqual(individual.country_id, ErspCountry.UZBEKISTAN)

    def test_journal_source_is_provider(self):
        card = provider_person.create(self.mosaic, provider_person.parse(person()), confirmed(), self.bekzod)
        entries = ChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(Individual), object_id=card.pk
        )
        self.assertTrue(entries.exists())
        self.assertEqual({entry.source for entry in entries}, {Source.PROVIDER})
        self.assertEqual(entries.filter(field="pinfl").get().user, self.bekzod)

    def test_bad_phone_rolls_everything_back(self):
        with self.assertRaises(ValidationError):
            provider_person.create(self.mosaic, provider_person.parse(person()), confirmed(phone="12"), self.bekzod)
        self.assertFalse(Counterparty.objects.exists())
        self.assertFalse(Individual.objects.exists())

    def test_names_and_search_follow_edits(self):
        card, individual = self.make_person()
        individual.first_name = "  YANGI   ISM "
        individual.save(by=self.bekzod)
        card.refresh_from_db()
        self.assertEqual(card.display_name, "FAMILIYA YANGI ISM")


class DatabaseRulesTests(IndividualCase):
    def test_same_pinfl_refused_in_form_check(self):
        self.make_person()
        card = Counterparty(insurance_company=self.mosaic, kind=Counterparty.Kind.INDIVIDUAL)
        card.save()
        second = Individual(counterparty=card, last_name="A", first_name="B", pinfl=PINFL,
                            birth_date=datetime.date(1991, 1, 1))
        with self.assertRaisesMessage(ValidationError, "Физлицо с таким ПИНФЛ уже есть"):
            second.full_clean()

    def test_same_pinfl_refused_by_database(self):
        self.make_person()
        card = Counterparty(insurance_company=self.mosaic, kind=Counterparty.Kind.INDIVIDUAL)
        card.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Individual(counterparty=card, last_name="A", first_name="B", pinfl=PINFL,
                       birth_date=datetime.date(1991, 1, 1)).save()

    def test_same_pinfl_in_other_company_is_fine(self):
        self.make_person()
        card, _ = self.make_person(company=self.other_company)
        self.assertEqual(card.individual.pinfl, PINFL)

    def test_pinfl_must_be_14_digits_in_database(self):
        _, individual = self.make_person()
        with connection.cursor() as cursor, self.assertRaises(IntegrityError), transaction.atomic():
            cursor.execute("UPDATE counterparties_individual SET pinfl = '123' WHERE counterparty_id = %s",
                           [individual.pk])

    def test_company_must_match_card(self):
        _, individual = self.make_person()
        with connection.cursor() as cursor, self.assertRaises(IntegrityError), transaction.atomic():
            cursor.execute("UPDATE counterparties_individual SET insurance_company_id = %s WHERE counterparty_id = %s",
                           [self.other_company.pk, individual.pk])

    def test_legal_entity_card_cannot_have_individual_data(self):
        card, _ = self.make()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Individual(counterparty=card, last_name="A", first_name="B", pinfl=PINFL,
                       birth_date=datetime.date(1991, 1, 1)).save()

    def test_form_check_for_legal_entity_card(self):
        card, _ = self.make()
        individual = Individual(counterparty=card, last_name="A", first_name="B", pinfl=PINFL,
                                birth_date=datetime.date(1991, 1, 1))
        with self.assertRaisesMessage(ValidationError, "только у карточки физического лица"):
            individual.full_clean()

    def test_names_required_in_database(self):
        _, individual = self.make_person()
        with connection.cursor() as cursor, self.assertRaises(IntegrityError), transaction.atomic():
            cursor.execute("UPDATE counterparties_individual SET first_name = '' WHERE counterparty_id = %s",
                           [individual.pk])

    def test_foreigner_cannot_have_pinfl(self):
        _, individual = self.make_person()
        individual.resident_type_id = ErspResidentType.FOREIGNER
        with self.assertRaisesMessage(ValidationError, "У иностранца не может быть ПИНФЛ"):
            individual.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            individual.save()  # и база не даст

    def test_foreigner_without_pinfl_is_fine_and_pinfl_not_unique_when_empty(self):
        for _ in range(2):  # два иностранца без ПИНФЛ в одной страховой
            card = Counterparty(insurance_company=self.mosaic, kind=Counterparty.Kind.INDIVIDUAL)
            card.save()
            person = Individual(counterparty=card, last_name="SMITH", first_name="JOHN", pinfl="",
                                birth_date=datetime.date(1980, 1, 1), passport_series="X", passport_number="1",
                                resident_type_id=ErspResidentType.FOREIGNER)
            person.full_clean()
            person.save()
        self.assertEqual(Individual.objects.filter(pinfl="").count(), 2)

    def test_gender_and_license_rules(self):
        _, individual = self.make_person()
        individual.gender = "x"
        with self.assertRaises(ValidationError):
            individual.full_clean()
        individual.gender = "m"
        individual.driver_license_series = " acl "
        individual.driver_license_number = "123 4567"
        individual.full_clean()
        individual.save()
        individual.refresh_from_db()
        self.assertEqual((individual.driver_license_series, individual.driver_license_number), ("ACL", "1234567"))

    def test_passport_normalized(self):
        _, individual = self.make_person()
        individual.passport_series = " ad "
        individual.passport_number = "00 00 000"
        individual.save()
        individual.refresh_from_db()
        self.assertEqual(individual.passport, "AD0000000")


class IndividualAdminTests(ErspMixin, IndividualCase):
    """Экран «Новое физлицо» и карточка физлица."""

    def setUp(self):
        super().setUp()
        self.fake = FakeErsp()
        original = ersp.urllib_transport
        ersp.urllib_transport = self.fake  # вместо ЕРСП - заглушка
        self.addCleanup(setattr, ersp, "urllib_transport", original)
        self.set_sender_pinfl(SENDER_PINFL)
        self.client.force_login(self.bekzod)

    def set_sender_pinfl(self, pinfl):
        # По одной записи и с сотрудником: массовое изменение журнал обходит и запрещено.
        self.bekzod.pinfl = pinfl
        self.bekzod.save(by=self.bekzod, update_fields=["pinfl"])

    def answer(self, result=None):
        self.fake.answers += [token_answer(), (200, {"error": 0, "error_message": "", "result": result or person()})]

    def login_staff(self, group):
        user = User.objects.create_user("ivanov", password=STRONG_PASSWORD, is_staff=True,
                                        insurance_company=self.mosaic, pinfl="30303030303030")
        give_roles(user, group)
        self.client.force_login(user)
        return user

    def query(self, **fields):
        return self.client.post(URL, {"action": "query", **fields})

    def test_button_in_list(self):
        self.assertContains(self.client.get(LIST_URL), "Новое физлицо")

    def test_empty_request_refused_without_calling_ersp(self):
        for fields in ({}, {"passport": "AD0000000"}, {"birth_date": "01.01.1990"}, {"pinfl": "123"}):
            page = self.query(**fields)
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, "errornote" if not fields.get("pinfl") else "ровно 14 цифр")
        self.assertEqual(self.fake.calls, [])

    def test_bad_passport_format_refused(self):
        page = self.query(passport="!!", birth_date="01.01.1990")
        self.assertContains(page, "латинские буквы и цифры")
        self.assertEqual(self.fake.calls, [])

    def test_by_passport_full_flow(self):
        self.assertContains(self.client.get(URL), "Запросить у провайдера")
        self.answer()
        response = self.query(passport=" ad 0000000 ", birth_date="01.01.1990")
        exchange = Exchange.objects.get(operation=Exchange.Operation.PROVIDER_PASSPORT)
        self.assertRedirects(response, f"{URL}?exchange={exchange.pk}")
        body = self.fake.calls[1]["body"]
        self.assertEqual((body["document"], body["birthDate"]), ("AD0000000", "1990-01-01"))
        self.assertEqual(body["senderPinfl"], SENDER_PINFL)

        preview = self.client.get(f"{URL}?exchange={exchange.pk}")
        self.assertContains(preview, "FAMILIYA")
        self.assertContains(preview, PINFL)
        self.assertContains(preview, "Завести физлицо")
        self.client.get(f"{URL}?exchange={exchange.pk}")  # обновление страницы
        self.assertEqual(len(self.fake.calls), 2)         # в ЕРСП не ушло ничего нового

        response = self.client.post(URL, {
            "exchange": exchange.pk, "action": "create", "passport_series": "AD", "passport_number": "0000000",
            "phone": "+998 (90) 123-45-67", "address": "Toshkent sh.", "region": 10,
            "resident_type": ErspResidentType.CITIZEN, "country": ErspCountry.UZBEKISTAN,
        })
        card = Counterparty.objects.get()
        self.assertRedirects(response, f"{LIST_URL}{card.pk}/change/", fetch_redirect_response=False)
        self.assertEqual(card.phone, "998901234567")
        self.assertEqual(card.individual.pinfl, PINFL)
        self.assertEqual(card.individual.country_id, ErspCountry.UZBEKISTAN)

    def test_by_pinfl(self):
        self.answer()
        response = self.query(pinfl=PINFL)
        exchange = Exchange.objects.get(operation=Exchange.Operation.PROVIDER_PINFL)
        self.assertRedirects(response, f"{URL}?exchange={exchange.pk}")
        self.assertEqual(self.fake.calls[1]["body"]["pinfl"], PINFL)

    def test_preview_prefilled_from_provider(self):
        self.answer()
        self.query(pinfl=PINFL)
        exchange = Exchange.objects.exclude(operation=Exchange.Operation.TOKEN).get()
        page = self.client.get(f"{URL}?exchange={exchange.pk}")
        self.assertContains(page, 'value="AD"')
        self.assertContains(page, 'value="0000000"')
        self.assertContains(page, "IIB Test")
        self.assertContains(page, "Toshkent sh., Test MFY, 1-uy")
        form = page.context["confirm"]
        self.assertEqual(form.initial["region"].pk, 10)
        self.assertEqual(form.initial["resident_type"].pk, ErspResidentType.CITIZEN)
        self.assertEqual(form.initial["country"].pk, ErspCountry.UZBEKISTAN)
        self.assertEqual(form.initial["gender"], "m")

    def test_known_pinfl_goes_to_card_without_asking(self):
        card, _ = self.make_person()
        response = self.query(pinfl=PINFL)
        self.assertRedirects(response, f"{LIST_URL}{card.pk}/change/", fetch_redirect_response=False)
        self.assertEqual(self.fake.calls, [])

    def test_known_pinfl_found_after_passport_query(self):
        card, _ = self.make_person()
        self.answer()
        response = self.query(passport="AD0000000", birth_date="01.01.1990", follow=False)
        exchange = Exchange.objects.exclude(operation=Exchange.Operation.TOKEN).get()
        again = self.client.get(f"{URL}?exchange={exchange.pk}")
        self.assertRedirects(again, f"{LIST_URL}{card.pk}/change/", fetch_redirect_response=False)
        self.assertEqual(Counterparty.objects.count(), 1)
        self.assertEqual(response.status_code, 302)

    def test_bad_answer_cannot_be_created(self):
        self.answer(person(currentPinfl="1", pinfls=[]))
        self.query(pinfl=PINFL)
        exchange = Exchange.objects.exclude(operation=Exchange.Operation.TOKEN).get()
        page = self.client.get(f"{URL}?exchange={exchange.pk}")
        self.assertContains(page, "нет верного ПИНФЛ")
        self.assertNotContains(page, "Завести физлицо")
        self.client.post(URL, {"exchange": exchange.pk, "action": "create"})
        self.assertFalse(Counterparty.objects.exists())

    def test_ersp_error_shown(self):
        self.fake.answers += [token_answer(), (200, {"error": 404, "error_message": "Не найдено"})]
        page = self.query(pinfl=PINFL)
        self.assertContains(page, "Провайдер ЕРСП: ЕРСП: Не найдено")
        self.assertFalse(Counterparty.objects.exists())

    def test_sender_without_pinfl_cannot_ask(self):
        self.set_sender_pinfl("")
        page = self.query(pinfl=PINFL)
        self.assertContains(page, "не заполнен ПИНФЛ")
        self.assertEqual(self.fake.calls, [])

    def test_create_with_bad_phone_shows_error_and_creates_nothing(self):
        self.answer()
        self.query(pinfl=PINFL)
        exchange = Exchange.objects.exclude(operation=Exchange.Operation.TOKEN).get()
        page = self.client.post(URL, {"exchange": exchange.pk, "action": "create", "phone": "12"})
        self.assertContains(page, "Физлицо не заведено")
        self.assertFalse(Counterparty.objects.exists())

    def test_foreign_exchange_not_usable(self):
        """Чужой ответ провайдера (другая страховая) применить нельзя."""
        stranger = Exchange.objects.create(
            insurance_company=self.other_company, environment="prod",
            operation=Exchange.Operation.PROVIDER_PINFL, method="POST", url="x",
            outcome=Exchange.Outcome.OK, response_body={"error": 0, "result": person()},
        )
        response = self.client.post(URL, {"exchange": stranger.pk, "action": "create"})
        self.assertRedirects(response, URL, fetch_redirect_response=False)
        self.assertFalse(Counterparty.objects.exists())

    def test_employee_view_only_cannot(self):
        self.login_staff(EMPLOYEE_GROUP)
        with refusal_expected(self):
            self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self.fake.calls, [])

    def test_reference_admin_may(self):
        self.login_staff(ADMIN_GROUP)
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_roles_see_individual_block_without_superuser(self):
        """Права на данные физлица выданы ролям миграцией (а не только суперпользователю)."""
        card, _ = self.make_person()
        for group, may_change in [(ADMIN_GROUP, True), (EMPLOYEE_GROUP, False), ("Работа с агентами", False)]:
            user = User.objects.create_user(f"u{abs(hash(group)) % 10**6}", password=STRONG_PASSWORD,
                                            is_staff=True, insurance_company=self.mosaic)
            give_roles(user, group)
            self.assertTrue(user.has_perm("counterparties.view_individual"), group)
            self.assertEqual(user.has_perm("counterparties.change_individual"), may_change, group)
            self.assertEqual(user.has_perm("counterparties.add_individual"), may_change, group)
            self.assertFalse(user.has_perm("counterparties.delete_individual"), group)
            if group != "Работа с агентами":  # у этой роли нет прав на сам экран контрагентов
                self.client.force_login(user)
                self.assertContains(self.client.get(f"{LIST_URL}{card.pk}/change/"), "Данные физлица")

    def test_card_page_shows_individual_blocks(self):
        card, _ = self.make_person()
        page = self.client.get(f"{LIST_URL}{card.pk}/change/")
        self.assertContains(page, "Данные физлица")
        self.assertContains(page, "Тип резидентства")
        self.assertNotContains(page, "Представители")  # у физлица их нет
        self.assertNotContains(page, "Сверить с провайдером ЕРСП")

    def test_legal_entity_card_unchanged(self):
        card, _ = self.make()
        page = self.client.get(f"{LIST_URL}{card.pk}/change/")
        self.assertContains(page, "Данные юрлица")
        self.assertNotContains(page, "Данные физлица")

    def test_list_and_search_by_pinfl(self):
        card, _ = self.make_person()
        self.make_person(company=self.other_company, pinfl="30101990000001")
        page = self.client.get(LIST_URL)
        self.assertContains(page, PINFL)
        self.assertNotContains(page, "30101990000001")  # чужая страховая
        found = self.client.get(LIST_URL, {"q": PINFL})
        self.assertContains(found, "FAMILIYA ISM")
        self.assertEqual(list(found.context["cl"].result_list), [card])
        found_by_name = self.client.get(LIST_URL, {"q": "familiya"})
        self.assertEqual(list(found_by_name.context["cl"].result_list), [card])

    def test_other_company_card_not_reachable(self):
        card, _ = self.make_person(company=self.other_company)
        response = self.client.get(f"{LIST_URL}{card.pk}/change/")
        self.assertEqual(response.status_code, 302)
