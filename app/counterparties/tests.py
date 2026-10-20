"""
Автотесты справочника контрагентов.

Банки, основания полномочий, должности, карточки контрагентов и данные
юрлица: формат, нормализация, защита в базе, мультиарендность,
стартовые списки, журнал изменений, админ-панель.

Запуск на сервере: bin/test counterparties
"""
import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from accounts.models import User
from audit.models import ChangeLog, Source
from companies.models import InsuranceCompany
from references.loader import load_from_files

from .models import Bank, Counterparty, LegalEntity, PowerBasis, Position
from .normalize import digits_only, remove_spaces, search_key, squash_spaces

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"


class NormalizeTests(SimpleTestCase):
    def test_squash_spaces(self):
        # Адрес Mosaic от провайдера: двойные пробелы внутри и пробелы в конце.
        self.assertEqual(
            squash_spaces("Choʻlpon MFY,  Elbek ko'chasi , 14-uy  "),
            "Choʻlpon MFY, Elbek ko'chasi , 14-uy",
        )

    def test_squash_non_breaking_space(self):
        # Неразрывный пробел (из Word, Excel) - тоже пробел.
        self.assertEqual(squash_spaces("ТОШКЕНТ Ш."), "ТОШКЕНТ Ш.")

    def test_remove_spaces(self):
        self.assertEqual(remove_spaces(" 010 95 "), "01095")

    def test_empty_values_kept(self):
        self.assertIsNone(squash_spaces(None))
        self.assertEqual(remove_spaces(""), "")

    def test_phone_digits_only(self):
        self.assertEqual(digits_only("+998 (99) 894-44-40"), "998998944440")

    def test_search_key_mixed_alphabets(self):
        # okedTitle от провайдера: в «Hаyotni» буква «а» - кириллическая.
        mixed = "Hаyotni sug`urtаlаsh"
        self.assertEqual(search_key(mixed), search_key("Hayotni sug'urtalash"))

    def test_search_key_quotes_and_apostrophes(self):
        self.assertEqual(search_key('"INSON" AJ ST'), "inson aj st")
        self.assertEqual(search_key("Choʻlpon"), search_key("Cho'lpon"))


class BankTests(TestCase):
    def test_valid_bank(self):
        bank = Bank(mfo="01095", name='ТОШКЕНТ Ш., "ASIA ALLIANCE BANK" АТ БАНКИ')
        bank.full_clean()
        bank.save()
        self.assertEqual(bank.inn, "")  # ИНН банка вводится позже
        self.assertTrue(bank.is_active)

    def test_spaces_removed_before_check(self):
        bank = Bank(mfo=" 01095 ", name="  Asia   Alliance Bank ", inn="123 456 789")
        bank.full_clean()  # не падает: пробелы - не ошибка
        bank.save()
        bank.refresh_from_db()
        self.assertEqual(bank.mfo, "01095")
        self.assertEqual(bank.name, "Asia Alliance Bank")
        self.assertEqual(bank.inn, "123456789")

    def test_mfo_format(self):
        for wrong in ["1095", "010950", "01O95"]:
            with self.subTest(mfo=wrong), self.assertRaises(ValidationError):
                Bank(mfo=wrong, name="Банк").full_clean()

    def test_inn_format(self):
        for wrong in ["12345678", "1234567890", "12345678A"]:
            with self.subTest(inn=wrong), self.assertRaises(ValidationError):
                Bank(mfo="01095", name="Банк", inn=wrong).full_clean()

    def test_mfo_unique(self):
        Bank(mfo="01095", name="Банк 1").save()
        with self.assertRaises(ValidationError):
            Bank(mfo="01095", name="Банк 2").full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Bank(mfo="01095", name="Банк 2").save()

    def test_same_inn_for_two_mfo_allowed(self):
        # Филиалы одного банка: МФО разные, ИНН может совпадать.
        Bank(mfo="00001", name="Банк, филиал 1", inn="123456789").save()
        Bank(mfo="00002", name="Банк, филиал 2", inn="123456789").save()

    def test_database_rejects_wrong_mfo(self):
        # Проверка продублирована в базе: обход формы не помогает.
        # _base_manager - намеренный обход запрета массовых операций (audit),
        # чтобы проверить саму базу; в рабочем коде так делать нельзя.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Bank._base_manager.bulk_create([Bank(mfo="ABCDE", name="Банк")])

    def test_database_rejects_wrong_inn(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Bank._base_manager.bulk_create([Bank(mfo="01095", name="Банк", inn="12345")])


class StarterListsTests(TestCase):
    """Стартовые списки из миграции 0002_starter_lists."""

    def test_power_bases(self):
        names = list(PowerBasis.objects.values_list("name", flat=True))
        self.assertEqual(len(names), 9)
        self.assertEqual(names[0], "Устав")
        self.assertEqual(names[-1], "Иное")  # «Иное» - последний пункт

    def test_positions(self):
        names = list(Position.objects.values_list("name", flat=True))
        self.assertEqual(len(names), 12)
        self.assertEqual(names[0], "Директор")
        self.assertEqual(names[-1], "Иное")

    def test_exactly_one_other_in_each_list(self):
        for model in [PowerBasis, Position]:
            with self.subTest(model=model.__name__):
                other = model.objects.get(is_other=True)
                self.assertEqual(other.name, "Иное")
                self.assertTrue(other.is_active)


class InputChoiceTests(TestCase):
    def test_new_item(self):
        item = Position(name="  Ректор ")
        item.full_clean()
        item.save()
        self.assertEqual(item.name, "Ректор")
        self.assertFalse(item.is_other)

    def test_name_unique_ignoring_case(self):
        with self.assertRaises(ValidationError):
            Position(name="директор").full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Position(name="ДИРЕКТОР").save()

    def test_second_other_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PowerBasis.objects.create(name="Другое", is_other=True)

    def test_other_cannot_be_switched_off(self):
        other = Position.objects.get(is_other=True)
        other.is_active = False
        with self.assertRaises(ValidationError):
            other.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            other.save()

    def test_ordinary_item_can_be_switched_off(self):
        item = Position.objects.get(name="Главный бухгалтер")
        item.is_active = False
        item.full_clean()
        item.save()


class AdminTests(TestCase):
    """Справочники в админ-панели: ввод, кто внёс, запрет удаления."""

    def setUp(self):
        self.admin = User.objects.create_superuser("bekzod", password=STRONG_PASSWORD)
        self.client.force_login(self.admin)

    def test_pages_open(self):
        for url in [
            "/admin/counterparties/bank/",
            "/admin/counterparties/bank/add/",
            "/admin/counterparties/powerbasis/",
            "/admin/counterparties/position/",
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_add_bank_records_who(self):
        response = self.client.post(
            "/admin/counterparties/bank/add/",
            {"mfo": " 01095", "name": "Asia  Alliance Bank", "inn": "", "is_active": "on"},
        )
        self.assertEqual(response.status_code, 302)  # сохранено, переход к списку
        bank = Bank.objects.get(mfo="01095")
        self.assertEqual(bank.name, "Asia Alliance Bank")
        self.assertEqual(bank.created_by, self.admin)
        self.assertEqual(bank.updated_by, self.admin)

    def test_wrong_mfo_shows_error(self):
        response = self.client.post(
            "/admin/counterparties/bank/add/", {"mfo": "1095", "name": "Банк"}
        )
        self.assertEqual(response.status_code, 200)  # форма вернулась с ошибкой
        self.assertContains(response, "МФО - ровно 5 цифр")
        self.assertFalse(Bank.objects.exists())

    def test_card_shows_who_and_when(self):
        bank = Bank(mfo="01095", name="Asia Alliance Bank")
        bank.save(by=self.admin)
        page = self.client.get(f"/admin/counterparties/bank/{bank.pk}/change/")
        self.assertContains(page, "Кто и когда")
        self.assertContains(page, "bekzod")

    def test_delete_forbidden_even_for_superuser(self):
        bank = Bank.objects.create(mfo="01095", name="Asia Alliance Bank")
        position = Position.objects.get(name="Директор")
        for url in [
            f"/admin/counterparties/bank/{bank.pk}/delete/",
            f"/admin/counterparties/position/{position.pk}/delete/",
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        self.assertTrue(Bank.objects.filter(pk=bank.pk).exists())
        self.assertTrue(Position.objects.filter(pk=position.pk).exists())

    def test_switching_off_other_shows_error(self):
        # Найдено при проверке на сервере 20.10: вместо сообщения была ошибка 500.
        # Поле «пункт «Иное»» в форме только для чтения, поэтому Django не
        # проверял правило «Иное нельзя выключить» в форме, и отказ приходил
        # уже из базы. Теперь правило проверяется и в форме.
        other = Position.objects.get(is_other=True)
        response = self.client.post(
            f"/admin/counterparties/position/{other.pk}/change/",
            {"name": "Иное", "sort_order": "999"},  # галочка «действует» снята
        )
        self.assertEqual(response.status_code, 200)  # форма вернулась с ошибкой, не 500
        self.assertContains(response, "Пункт «Иное» нельзя выключить")
        other.refresh_from_db()
        self.assertTrue(other.is_active)

    def test_other_flag_not_editable(self):
        # «Иное» нельзя назначить из админ-панели: поле только для чтения.
        self.client.post(
            "/admin/counterparties/position/add/",
            {"name": "Ректор", "is_other": "on", "sort_order": "100", "is_active": "on"},
        )
        self.assertFalse(Position.objects.get(name="Ректор").is_other)


# --- Контрагенты --------------------------------------------------------------------

# Ответ провайдера ЕРСП по Inson (docs/requirements/counterparties-legal-entities.md).
INSON = {
    "inn": "207078596",
    "full_name": '"INSON" AKSIYADORLIK JAMIYATI SUG\'URTA TASHKILOTI',
    "short_name": '"INSON" AJ ST',
    "oked_id": "65120",
    "okpo": "23047346",
    "soato_code": "1726277",
    "soato_name": "Шайхантахурский район",
    "reg_certificate_number": "82",
    "reg_certificate_date": datetime.date(2010, 5, 12),
    "charter_capital": Decimal("85751457400"),
}


class CounterpartyTestCase(TestCase):
    """Общая подготовка: справочники ЕРСП, две страховые, сотрудник Mosaic."""

    def setUp(self):
        load_from_files()
        self.mosaic = InsuranceCompany.objects.create(
            name="Mosaic Insurance Group", code="mosaic", ersp_insurer_id=1084, ersp_region_id=10
        )
        self.other_company = InsuranceCompany.objects.create(
            name="Test Insurance KZ", code="test-kz", country="KZ"
        )
        self.bekzod = User.objects.create_superuser(
            "bekzod", password=STRONG_PASSWORD, insurance_company=self.mosaic
        )

    def make(self, company=None, by=None, counterparty_fields=None, **legal_fields):
        """Создать контрагента-юрлицо так же, как это делает админ-панель."""
        counterparty = Counterparty(
            insurance_company=company or self.mosaic, **(counterparty_fields or {})
        )
        counterparty.full_clean()
        counterparty.save(by=by)
        legal = LegalEntity(counterparty=counterparty, **{**INSON, **legal_fields})
        legal.full_clean()
        legal.save(by=by)
        counterparty.refresh_from_db()
        return counterparty, legal


class LegalEntityTests(CounterpartyTestCase):
    def test_inson_from_provider_response(self):
        counterparty, legal = self.make(counterparty_fields={
            "phone": "998998944440",
            "address": "Labzak MFY, Zulfiyaxonim ko'chasi, 12-uy  ",
        })
        self.assertEqual(counterparty.display_name, '"INSON" AJ ST')
        self.assertEqual(counterparty.kind, Counterparty.Kind.LEGAL_ENTITY)
        self.assertEqual(counterparty.status, Counterparty.Status.ACTIVE)
        self.assertEqual(counterparty.address, "Labzak MFY, Zulfiyaxonim ko'chasi, 12-uy")
        self.assertEqual(counterparty.region_id, 10)  # город Ташкент - по СОАТО 1726…
        legal.refresh_from_db()
        self.assertEqual(legal.country, "UZ")
        self.assertEqual(legal.insurance_company, self.mosaic)
        self.assertEqual(legal.charter_capital, Decimal("85751457400.00"))
        self.assertEqual(legal.charter_capital_currency_id, 860)  # сум - по умолчанию
        self.assertEqual(
            str(legal.oked), "65120 - Hayotni sug‘urtalashdan tashqari sug‘urtalash"
        )  # название - из справочника ЕРСП

    def test_full_name_shown_when_no_short_name(self):
        counterparty, _ = self.make(short_name="")
        self.assertEqual(counterparty.display_name, INSON["full_name"])

    def test_inn_and_full_name_required(self):
        counterparty = Counterparty(insurance_company=self.mosaic)
        counterparty.save()
        with self.assertRaises(ValidationError) as error:
            LegalEntity(counterparty=counterparty).full_clean()
        self.assertIn("inn", error.exception.message_dict)
        self.assertIn("full_name", error.exception.message_dict)

    def test_everything_else_may_be_empty(self):
        # Карточку можно сохранить неполной: обязательны только ИНН и наименование.
        empty = {field: "" for field in ["short_name", "okpo", "soato_code", "soato_name",
                                         "reg_certificate_number"]}
        counterparty, legal = self.make(
            **empty, oked_id=None, reg_certificate_date=None, charter_capital=None
        )
        self.assertIsNone(counterparty.region)
        self.assertIsNone(legal.charter_capital_currency)  # нет капитала - нет и валюты

    def test_inn_spaces_removed(self):
        _, legal = self.make(inn=" 207 078 596 ")
        self.assertEqual(legal.inn, "207078596")

    def test_local_inn_format(self):
        for wrong in ["20707859", "2070785960", "20707859A"]:
            with self.subTest(inn=wrong), self.assertRaises(ValidationError):
                self.make(inn=wrong)

    def test_foreign_inn(self):
        _, legal = self.make(inn="kz 1234 5678 90", is_foreign=True, country="kz")
        self.assertEqual(legal.inn, "KZ1234567890")
        self.assertEqual(legal.country, "KZ")

    def test_foreign_inn_too_short(self):
        with self.assertRaises(ValidationError):
            self.make(inn="AB12345", is_foreign=True, country="KZ")

    def test_foreign_needs_other_country(self):
        with self.assertRaises(ValidationError):
            self.make(inn="KZ1234567890", is_foreign=True, country="UZ")

    def test_local_country_always_uz(self):
        _, legal = self.make(country="KZ")
        self.assertEqual(legal.country, "UZ")

    def test_codes_format(self):
        for field, wrong in [("okpo", "2304734"), ("soato_code", "17262")]:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.make(**{field: wrong})

    def test_oked_must_be_in_ersp_reference(self):
        with self.assertRaises(ValidationError) as error:
            self.make(oked_id="99999")
        self.assertIn("oked", error.exception.message_dict)

    def test_other_currency_kept(self):
        _, legal = self.make(charter_capital_currency_id=840)
        self.assertEqual(legal.charter_capital_currency.ticker, "USD")

    def test_capital_needs_currency_in_database(self):
        # Обход кода (без подстановки сума) - база не примет капитал без валюты.
        _, legal = self.make()
        with self.assertRaises(IntegrityError), transaction.atomic():
            LegalEntity._base_manager.filter(pk=legal.pk).update(charter_capital_currency=None)

    def test_negative_capital_rejected(self):
        with self.assertRaises(ValidationError):
            self.make(charter_capital=Decimal("-1"))

    def test_region_not_overwritten(self):
        # Регион, выбранный сотрудником, СОАТО не заменяет.
        counterparty, _ = self.make(counterparty_fields={"region_id": 14})
        self.assertEqual(counterparty.region_id, 14)

    def test_unknown_soato_leaves_region_empty(self):
        counterparty, _ = self.make(soato_code="9999999")
        self.assertIsNone(counterparty.region)


class PhoneTests(CounterpartyTestCase):
    def test_local_phone_normalized(self):
        counterparty, _ = self.make(counterparty_fields={"phone": "+998 (99) 894-44-40"})
        self.assertEqual(counterparty.phone, "998998944440")

    def test_local_phone_must_be_998_and_9_digits(self):
        with self.assertRaises(ValidationError) as error:
            self.make(counterparty_fields={"phone": "77012345678"})
        self.assertIn("998", str(error.exception))

    def test_foreign_phone_international(self):
        counterparty, _ = self.make(
            inn="KZ1234567890", is_foreign=True, country="KZ",
            counterparty_fields={"phone": "+7 701 234 56 78"},
        )
        self.assertEqual(counterparty.phone, "77012345678")

    def test_phone_optional(self):
        counterparty, _ = self.make(inn="KZ1234567890", is_foreign=True, country="KZ")
        self.assertEqual(counterparty.phone, "")


class DuplicateInnTests(CounterpartyTestCase):
    def test_same_inn_same_company_rejected_with_name(self):
        self.make()
        with self.assertRaises(ValidationError) as error:
            self.make()
        self.assertIn('"INSON" AJ ST', error.exception.message_dict["inn"][0])

    def test_database_also_rejects(self):
        # Даже без проверки формы (сразу save) база не даст завести дубль.
        self.make()
        counterparty = Counterparty(insurance_company=self.mosaic)
        counterparty.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            LegalEntity(counterparty=counterparty, **INSON).save()

    def test_same_inn_in_other_company_allowed(self):
        # Клиенты разных страховых не пересекаются.
        self.make()
        self.make(company=self.other_company)

    def test_archived_counterparty_still_holds_inn(self):
        # Архив не освобождает ИНН: клиента возвращают из архива, а не заводят снова.
        counterparty, _ = self.make()
        counterparty.status = Counterparty.Status.ARCHIVED
        counterparty.save()
        with self.assertRaises(ValidationError):
            self.make()


class SameCompanyTests(CounterpartyTestCase):
    """Составная ссылка в базе: страховая у карточки и у данных юрлица одна."""

    def test_legal_entity_company_cannot_diverge(self):
        _, legal = self.make()
        # _base_manager - намеренный обход запретов кода, чтобы проверить базу.
        with self.assertRaises(IntegrityError), transaction.atomic():
            LegalEntity._base_manager.filter(pk=legal.pk).update(
                insurance_company=self.other_company
            )

    def test_counterparty_company_cannot_change(self):
        counterparty, _ = self.make()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Counterparty._base_manager.filter(pk=counterparty.pk).update(
                insurance_company=self.other_company
            )

    def test_company_copied_from_card(self):
        _, legal = self.make(company=self.other_company)
        self.assertEqual(legal.insurance_company, self.other_company)


class SearchTests(CounterpartyTestCase):
    def test_search_key_built_from_names_and_inn(self):
        counterparty, _ = self.make()
        self.assertIn("inson aj st", counterparty.search_text)
        self.assertIn("207078596", counterparty.search_text)

    def test_mixed_alphabets_found(self):
        # «о» в «Tоshkent» - кириллическая, ищем латиницей.
        counterparty, _ = self.make(full_name="Tоshkent Trade MChJ", short_name="")
        found = Counterparty.objects.filter(search_text__icontains=search_key("toshkent"))
        self.assertEqual(list(found), [counterparty])


class CounterpartyJournalTests(CounterpartyTestCase):
    def entries(self, model, pk):
        return ChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(model), object_id=pk
        )

    def test_creation_logged_with_company(self):
        counterparty, _ = self.make(by=self.bekzod)
        fields = set(self.entries(LegalEntity, counterparty.pk).values_list("field", flat=True))
        self.assertIn("inn", fields)
        self.assertIn("charter_capital", fields)
        entry = self.entries(LegalEntity, counterparty.pk).get(field="inn")
        self.assertEqual(entry.insurance_company, self.mosaic)
        self.assertEqual(entry.user, self.bekzod)
        self.assertEqual(entry.new_value, "207078596")

    def test_system_fields_not_logged(self):
        # Наименование для списка и ключ для поиска собирает система -
        # в журнал пишутся исходные наименования, а не они.
        counterparty, _ = self.make(by=self.bekzod)
        fields = set(self.entries(Counterparty, counterparty.pk).values_list("field", flat=True))
        self.assertNotIn("display_name", fields)
        self.assertNotIn("search_text", fields)

    def test_region_from_soato_logged_as_system(self):
        counterparty, _ = self.make(by=self.bekzod)
        entry = self.entries(Counterparty, counterparty.pk).get(field="region")
        self.assertEqual(entry.source, Source.SYSTEM)
        self.assertEqual(entry.new_value, 10)

    def test_capital_logged_exactly(self):
        counterparty, legal = self.make(by=self.bekzod)
        legal.charter_capital = Decimal("140556531000.50")
        legal.save(by=self.bekzod)
        entry = self.entries(LegalEntity, counterparty.pk).get(
            field="charter_capital", action="update"
        )
        self.assertEqual(Decimal(entry.old_value), Decimal("85751457400"))
        self.assertEqual(Decimal(entry.new_value), Decimal("140556531000.50"))


class CounterpartyAdminTests(CounterpartyTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.bekzod)

    def post_data(self, counterparty=None, **legal):
        """Данные формы карточки так, как их отправляет браузер."""
        values = {**INSON, **legal}
        data = {
            "phone": "998998944440",
            "email": "",
            "region": "",
            "address": "Labzak MFY",
            "status": "active",
            "legal_entity-TOTAL_FORMS": "1",
            "legal_entity-INITIAL_FORMS": "1" if counterparty else "0",
            "legal_entity-MIN_NUM_FORMS": "1",
            "legal_entity-MAX_NUM_FORMS": "1",
            "legal_entity-0-country": "UZ",
            "legal_entity-0-charter_capital_currency": "860",
        }
        for field, value in values.items():
            if field == "oked_id":
                field = "oked"
            if field == "is_foreign":
                if value:
                    data["legal_entity-0-is_foreign"] = "on"
                continue
            data[f"legal_entity-0-{field}"] = str(value)
        if counterparty:
            data["legal_entity-0-counterparty"] = str(counterparty.pk)
        return data

    def test_pages_open(self):
        counterparty, _ = self.make()
        for url in ["/admin/counterparties/counterparty/",
                    "/admin/counterparties/counterparty/add/",
                    f"/admin/counterparties/counterparty/{counterparty.pk}/change/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_create_through_admin(self):
        response = self.client.post("/admin/counterparties/counterparty/add/", self.post_data())
        self.assertEqual(response.status_code, 302)
        counterparty = Counterparty.objects.get()
        self.assertEqual(counterparty.insurance_company, self.mosaic)  # из учётной записи
        self.assertEqual(counterparty.display_name, '"INSON" AJ ST')
        self.assertEqual(counterparty.created_by, self.bekzod)
        self.assertEqual(counterparty.legal_entity.created_by, self.bekzod)
        self.assertTrue(ChangeLog.objects.filter(field="inn", user=self.bekzod).exists())

    def test_duplicate_inn_shows_message(self):
        # Ловушка из AGENTS.md: страховой нет в форме, но повтор ИНН должен
        # показаться сообщением, а не ошибкой 500.
        self.make()
        response = self.client.post("/admin/counterparties/counterparty/add/", self.post_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Контрагент с таким ИНН уже есть")
        self.assertEqual(Counterparty.objects.count(), 1)

    def test_wrong_local_phone_shows_message(self):
        data = self.post_data()
        data["phone"] = "77012345678"
        response = self.client.post("/admin/counterparties/counterparty/add/", data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "998 и 9 цифр")
        self.assertFalse(Counterparty.objects.exists())

    def test_company_field_hidden_for_company_employee(self):
        page = self.client.get("/admin/counterparties/counterparty/add/")
        self.assertNotContains(page, 'name="insurance_company"')

    def test_other_company_not_visible(self):
        foreign_client, _ = self.make(company=self.other_company, short_name="Чужой клиент")
        listing = self.client.get("/admin/counterparties/counterparty/")
        self.assertNotContains(listing, "Чужой клиент")
        page = self.client.get(f"/admin/counterparties/counterparty/{foreign_client.pk}/change/")
        self.assertEqual(page.status_code, 302)  # «такой записи нет» и возврат на главную

    def test_system_admin_sees_all_and_chooses_company(self):
        root = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        self.client.force_login(root)
        self.make(company=self.other_company, short_name="Чужой клиент")
        self.assertContains(self.client.get("/admin/counterparties/counterparty/"), "Чужой клиент")
        self.assertContains(
            self.client.get("/admin/counterparties/counterparty/add/"), 'name="insurance_company"'
        )

    def test_search_by_name_and_inn(self):
        self.make()
        for query in ["inson", "INSON AJ", "207078596", "207 078 596"]:
            with self.subTest(query=query):
                listing = self.client.get("/admin/counterparties/counterparty/", {"q": query})
                self.assertContains(listing, "AJ ST")
        nothing = self.client.get("/admin/counterparties/counterparty/", {"q": "Mosaic"})
        self.assertNotContains(nothing, "AJ ST")

    def test_change_through_admin_shown_in_history(self):
        counterparty, _ = self.make(by=self.bekzod)
        response = self.client.post(
            f"/admin/counterparties/counterparty/{counterparty.pk}/change/",
            self.post_data(counterparty, oked="65200"),
        )
        self.assertEqual(response.status_code, 302)
        history = self.client.get(
            f"/admin/counterparties/counterparty/{counterparty.pk}/history/", follow=True
        )
        self.assertContains(history, "ОКЭД")
        self.assertContains(history, "65200")

    def test_cleared_region_restored_from_soato(self):
        # Найдено Бекзодом 20.10: регион очистили в карточке, данные юрлица не
        # трогали - и регион по СОАТО не подставился. Теперь подставляется.
        counterparty, _ = self.make(by=self.bekzod)
        self.assertEqual(counterparty.region_id, 10)
        data = self.post_data(counterparty)
        data["region"] = ""
        response = self.client.post(
            f"/admin/counterparties/counterparty/{counterparty.pk}/change/", data
        )
        self.assertEqual(response.status_code, 302)
        counterparty.refresh_from_db()
        self.assertEqual(counterparty.region_id, 10)
        # В журнале честно: сотрудник очистил, система вернула. (Первая запись
        # о регионе - подстановка при создании карточки, её не смотрим.)
        changes = list(
            ChangeLog.objects.filter(field="region", action="update")
            .order_by("id")
            .values_list("source", "old_value", "new_value")
        )
        self.assertEqual(changes[-2:], [(Source.MANUAL, 10, None), (Source.SYSTEM, None, 10)])

    def test_no_delete(self):
        counterparty, _ = self.make()
        response = self.client.post(
            f"/admin/counterparties/counterparty/{counterparty.pk}/delete/", {"post": "yes"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Counterparty.objects.filter(pk=counterparty.pk).exists())
