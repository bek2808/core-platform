"""
Автотесты заполнения карточки по ИНН от провайдера ЕРСП
(counterparties/provider.py и страницы в админ-панели).

Разбор ответа, сверка «у нас | у провайдера», применение с журналом
(источник «провайдер ЕРСП»), смена руководителя, новый счёт и банк,
новая карточка по ИНН, права и мультиарендность.

В сеть тесты не ходят: ЕРСП заменён заглушкой FakeErsp (integrations/tests.py).

Запуск на сервере: bin/test counterparties
"""
import copy
import datetime
from decimal import Decimal

from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from accounts.models import User
from audit.models import ChangeLog, Source
from config.testing import refusal_expected
from integrations import ersp
from integrations.models import Exchange
from integrations.tests import FakeErsp, SecretsMixin, token_answer

from . import provider
from .models import Bank, BankAccount, Counterparty, LegalEntity, Representative
from .tests import ADMIN_GROUP, EMPLOYEE_GROUP, STRONG_PASSWORD, CounterpartyTestCase

# Полный ответ провайдера по Inson ("result"), как в требованиях Бекзода.
INSON_RESULT = {
    "name_short": "\"INSON\" AJ ST",
    "nameShort": "\"INSON\" AJ ST",
    "phoneNumber": None,
    "phone_number": None,
    "fund": 85751457400,
    "gd_full_name": "NIYAZMETOV ILXOMJON IKRAMBAYEVICH",
    "gdFullName": "NIYAZMETOV ILXOMJON IKRAMBAYEVICH",
    "district_soato_code": "1726277",
    "districtSoatoCode": "1726277",
    "district_soato_name": "Шайхантахурский район",
    "districtSoatoName": "Шайхантахурский район",
    "bank_mfo": "01095",
    "bankMfo": "01095",
    "bank_name": "ТОШКЕНТ Ш., \"ASIA ALLIANCE BANK\" АТ БАНКИ",
    "bankName": "ТОШКЕНТ Ш., \"ASIA ALLIANCE BANK\" АТ БАНКИ",
    "account": "20216000004849419001",
    "okpo": "23047346",
    "name": "\"INSON\" AKSIYADORLIK JAMIYATI SUG'URTA TASHKILOTI",
    "reg_number": "82",
    "regCertificate": "82",
    "reg_date": "2010-05-12",
    "regCertificateIssueDate": "2010-05-12",
    "email": "",
    "address": "Labzak MFY, Zulfiyaxonim ko'chasi, 12-uy  ",
    "district": "Ҳудудлараро инспекцияси",
    "phone": "998998944440",
    "oked": "65120",
    "oked_title": "Hаyotni sug`urtаlаshdаn tаshqаri sug`urtаlаsh",
    "okedTitle": "Hаyotni sug`urtаlаshdаn tаshqаri sug`urtаlаsh",
}
INN = "207078596"
DIRECTOR = "NIYAZMETOV ILXOMJON IKRAMBAYEVICH"
ACCOUNT = "20216000004849419001"


def inson(**changes):
    result = copy.deepcopy(INSON_RESULT)
    result.update(changes)
    return result


def statuses(rows):
    return {row.key: row.status for row in rows}


class ProviderCase(CounterpartyTestCase):
    def empty_card(self, **legal_fields):
        """Карточка Inson, в которой есть только ИНН и наименование."""
        fields = {k: None for k in ("oked_id", "reg_certificate_date", "charter_capital")}
        fields.update({k: "" for k in ("short_name", "okpo", "soato_code", "soato_name",
                                       "reg_certificate_number")})
        fields.update(legal_fields)
        card, _ = self.make(**fields)
        return card

    def journal(self, model, obj):
        return ChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(model), object_id=obj.pk
        )


class ParseTests(ProviderCase):
    def test_inson_cleaned(self):
        data = provider.parse(INN, inson())
        values = {key: value.value for key, value in data.values.items()}
        self.assertEqual(values["address"], "Labzak MFY, Zulfiyaxonim ko'chasi, 12-uy")  # без пробелов в конце
        self.assertEqual(values["phone"], "998998944440")
        self.assertEqual(values["oked"], "65120")
        self.assertEqual(values["okpo"], "23047346")
        self.assertEqual(values["soato_code"], "1726277")
        self.assertEqual(values["reg_certificate_number"], "82")
        self.assertEqual(values["reg_certificate_date"], datetime.date(2010, 5, 12))
        self.assertEqual(values["charter_capital"], (Decimal("85751457400.00"), 860))
        self.assertEqual(data.values["charter_capital"].shown, "85 751 457 400.00 сум")
        self.assertFalse(data.values["email"].usable)  # пусто - не значение
        self.assertEqual(data.director.value, DIRECTOR)
        self.assertEqual(data.account_number.value, ACCOUNT)
        self.assertEqual(data.bank_mfo, "01095")
        self.assertEqual(data.warnings, [])

    def test_twins_disagree(self):
        data = provider.parse(INN, inson(name_short="ДРУГОЕ"))
        self.assertEqual(data.values["short_name"].value, '"INSON" AJ ST')  # взято camelCase
        self.assertIn("nameShort и name_short", data.warnings[0])

    def test_unknown_oked_not_usable(self):
        value = provider.parse(INN, inson(oked="99999")).values["oked"]
        self.assertFalse(value.usable)
        self.assertIn("нет в справочнике ОКЭД ЕРСП", value.problem)

    def test_bad_formats_not_usable(self):
        data = provider.parse(INN, inson(okpo="123", phone="12345", email="не почта",
                                         regCertificateIssueDate="12.05.2010", account="123"))
        for key in ("okpo", "phone", "email", "reg_certificate_date"):
            self.assertTrue(data.values[key].problem, key)
        self.assertIn("не 20 цифр", data.account_number.problem)

    def test_account_without_mfo(self):
        self.assertIn("Нет МФО", provider.parse(INN, inson(bankMfo="")).account_number.problem)


class CompareTests(ProviderCase):
    def test_new_card_everything_to_fill(self):
        rows = provider.compare(None, provider.parse(INN, inson()))
        result = statuses(rows)
        self.assertEqual(result.pop("email"), provider.NO_DATA)
        self.assertEqual(set(result.values()), {provider.FILL})
        self.assertTrue(all(row.preselected for row in rows if row.status == provider.FILL))

    def test_card_entered_from_same_answer_matches(self):
        # Inson в рабочей базе введён вручную по этому же ответу.
        card, _ = self.make(counterparty_fields={
            "phone": "998998944440", "address": "Labzak MFY, Zulfiyaxonim ko'chasi, 12-uy",
        })
        Representative(counterparty=card, full_name=DIRECTOR, is_primary=True).save()
        BankAccount(counterparty=card, bank=Bank.objects.create(mfo="01095", name="AAB"),
                    number=ACCOUNT, is_primary=True).save()
        result = statuses(provider.compare(card, provider.parse(INN, inson())))
        self.assertEqual(result.pop("email"), provider.NO_DATA)
        self.assertEqual(set(result.values()), {provider.SAME})

    def test_empty_ours_filled_different_ours_offered(self):
        card, _ = self.make(okpo="", counterparty_fields={"phone": "998901234567"})
        rows = {row.key: row for row in provider.compare(card, provider.parse(INN, inson()))}
        self.assertEqual(rows["okpo"].status, provider.FILL)
        self.assertTrue(rows["okpo"].preselected)
        self.assertEqual(rows["phone"].status, provider.DIFF)
        self.assertFalse(rows["phone"].preselected)  # отличие - только если отметить
        self.assertEqual(rows["phone"].ours, "998901234567")

    def test_provider_empty_keeps_ours(self):
        card, _ = self.make(counterparty_fields={"email": "office@inson.uz"})
        row = {r.key: r for r in provider.compare(card, provider.parse(INN, inson()))}["email"]
        self.assertEqual(row.status, provider.NO_DATA)
        self.assertFalse(row.selectable)

    def test_director_same_regardless_of_case_and_lookalikes(self):
        card, _ = self.make()
        # «Н», «А», «О», «Е» ... кириллицей, регистр другой.
        name = "Niyazmetov Ilxomjon Ikrambayevich".replace("a", "а").replace("o", "о").replace("e", "е")
        self.assertNotEqual(name.lower(), DIRECTOR.lower())  # буквы правда кириллические
        Representative(counterparty=card, full_name=name, is_primary=True).save()
        rows = statuses(provider.compare(card, provider.parse(INN, inson())))
        self.assertEqual(rows["director"], provider.SAME)

    def test_director_changed(self):
        card, _ = self.make()
        Representative(counterparty=card, full_name="ALIYEV VALI", is_primary=True).save()
        row = {r.key: r for r in provider.compare(card, provider.parse(INN, inson()))}["director"]
        self.assertEqual(row.status, provider.DIFF)
        self.assertIn("закроется сегодняшней датой", row.note)

    def test_account_variants(self):
        card, _ = self.make()
        rows = {r.key: r for r in provider.compare(card, provider.parse(INN, inson()))}
        self.assertEqual(rows["account"].status, provider.FILL)  # основного нет
        self.assertIn("будет заведён", rows["account"].note)       # банка 01095 нет
        bank = Bank.objects.create(mfo="00842", name="KDB")
        BankAccount(counterparty=card, bank=bank, number="20214000105338303001", is_primary=True).save()
        rows = {r.key: r for r in provider.compare(card, provider.parse(INN, inson()))}
        self.assertEqual(rows["account"].status, provider.DIFF)
        self.assertIn("неосновным", rows["account"].note)


class ApplyTests(ProviderCase):
    def test_fill_and_journal_source_provider(self):
        card = self.empty_card()
        data = provider.parse(INN, inson())
        done = provider.apply(card, data, ["okpo", "oked", "phone", "address", "charter_capital",
                                           "reg_certificate_date", "soato_code"], self.bekzod)
        self.assertIn("ОКПО", done)
        card.refresh_from_db()
        legal = LegalEntity.objects.get(pk=card.pk)
        self.assertEqual(legal.okpo, "23047346")
        self.assertEqual(legal.oked_id, "65120")
        self.assertEqual(legal.charter_capital, Decimal("85751457400.00"))
        self.assertEqual(legal.charter_capital_currency_id, 860)
        self.assertEqual(legal.reg_certificate_date, datetime.date(2010, 5, 12))
        self.assertEqual(card.phone, "998998944440")
        self.assertEqual(card.address, "Labzak MFY, Zulfiyaxonim ko'chasi, 12-uy")
        self.assertEqual(card.region_id, 10)  # по СОАТО, система
        entry = self.journal(LegalEntity, legal).get(field="okpo")
        self.assertEqual((entry.source, entry.user), (Source.PROVIDER, self.bekzod))
        self.assertEqual(self.journal(Counterparty, card).get(field="phone").source, Source.PROVIDER)
        self.assertEqual(self.journal(Counterparty, card).get(field="region").source, Source.SYSTEM)

    def test_only_selected_and_never_invalid(self):
        card, _ = self.make(okpo="", counterparty_fields={"phone": "998901234567"})
        data = provider.parse(INN, inson(oked="99999"))
        provider.apply(card, data, ["oked"], self.bekzod)  # oked негодный, okpo не отмечен
        legal = LegalEntity.objects.get(pk=card.pk)
        self.assertEqual(legal.okpo, "")
        self.assertEqual(legal.oked_id, "65120")
        card.refresh_from_db()
        self.assertEqual(card.phone, "998901234567")

    def test_diff_applied_when_selected(self):
        card, _ = self.make(counterparty_fields={"phone": "998901234567"})
        provider.apply(card, provider.parse(INN, inson()), ["phone"], self.bekzod)
        card.refresh_from_db()
        self.assertEqual(card.phone, "998998944440")
        entry = self.journal(Counterparty, card).get(field="phone", source=Source.PROVIDER)
        self.assertEqual((entry.old_value, entry.new_value), ("998901234567", "998998944440"))

    def test_new_director_closes_old(self):
        card, _ = self.make()
        old = Representative(counterparty=card, full_name="ALIYEV VALI", is_primary=True,
                             pinfl="12345678901234")
        old.save()
        provider.apply(card, provider.parse(INN, inson()), ["director"], self.bekzod)
        old.refresh_from_db()
        self.assertFalse(old.is_primary)
        self.assertEqual(old.valid_to, timezone.localdate())
        new = card.representatives.get(is_primary=True)
        self.assertEqual(new.full_name, DIRECTOR)
        self.assertEqual(self.journal(Representative, new).first().source, Source.PROVIDER)

    def test_known_representative_becomes_primary(self):
        card, _ = self.make()
        deputy = Representative(counterparty=card, full_name="ALIYEV VALI", is_primary=True)
        deputy.save()
        director = Representative(counterparty=card, full_name=DIRECTOR)
        director.save()
        provider.apply(card, provider.parse(INN, inson()), ["director"], self.bekzod)
        deputy.refresh_from_db()
        director.refresh_from_db()
        self.assertTrue(director.is_primary)
        self.assertFalse(deputy.is_primary)
        self.assertIsNone(deputy.valid_to)  # заместитель не закрыт - он по-прежнему представитель
        self.assertEqual(card.representatives.count(), 2)

    def test_account_creates_bank(self):
        card, _ = self.make()
        done = provider.apply(card, provider.parse(INN, inson()), ["account"], self.bekzod)
        bank = Bank.objects.get(mfo="01095")
        self.assertEqual(bank.name, 'ТОШКЕНТ Ш., "ASIA ALLIANCE BANK" АТ БАНКИ')
        self.assertEqual(bank.inn, "")
        account = card.accounts.get()
        self.assertEqual((account.number, account.bank, account.is_primary), (ACCOUNT, bank, True))
        self.assertEqual(account.currency_id, 860)
        self.assertIn("допишите его ИНН", done[-1])
        self.assertEqual(self.journal(Bank, bank).first().source, Source.PROVIDER)

    def test_account_added_not_primary_when_primary_exists(self):
        card, _ = self.make()
        Bank.objects.create(mfo="01095", name="AAB")
        kdb = Bank.objects.create(mfo="00842", name="KDB")
        BankAccount(counterparty=card, bank=kdb, number="20214000105338303001", is_primary=True).save()
        provider.apply(card, provider.parse(INN, inson()), ["account"], self.bekzod)
        self.assertEqual(Bank.objects.filter(mfo="01095").count(), 1)  # банк не задвоился
        added = card.accounts.get(number=ACCOUNT)
        self.assertFalse(added.is_primary)
        self.assertTrue(card.accounts.get(number="20214000105338303001").is_primary)

    def test_create_new_card(self):
        data = provider.parse(INN, inson())
        selected = [row.key for row in provider.compare(None, data) if row.preselected]
        card = provider.create(self.mosaic, data, selected, self.bekzod)
        legal = card.legal_entity
        self.assertEqual(legal.inn, INN)
        self.assertEqual(card.display_name, '"INSON" AJ ST')
        self.assertEqual(card.insurance_company, self.mosaic)
        self.assertEqual(card.region_id, 10)
        self.assertEqual(card.representatives.get(is_primary=True).full_name, DIRECTOR)
        self.assertEqual(card.accounts.get(is_primary=True).number, ACCOUNT)
        self.assertEqual(self.journal(LegalEntity, legal).get(field="inn").source, Source.PROVIDER)

    def test_create_without_name_refused(self):
        with self.assertRaises(provider.ProviderError):
            provider.create(self.mosaic, provider.parse(INN, inson(name="")), [], self.bekzod)
        self.assertFalse(Counterparty.objects.exists())


class ProviderAdminTests(SecretsMixin, ProviderCase):
    """Страницы «Сверить с провайдером» и «Новый по ИНН»."""

    def setUp(self):
        super().setUp()
        self.fake = FakeErsp()
        original = ersp.urllib_transport
        ersp.urllib_transport = self.fake  # вместо ЕРСП - заглушка
        self.addCleanup(setattr, ersp, "urllib_transport", original)
        self.client.force_login(self.bekzod)

    def answer(self, result=None):
        self.fake.answers += [token_answer(), (200, {"error": 0, "error_message": "", "result": result or inson()})]

    def login_staff(self, group):
        user = User.objects.create_user("ivanov", password=STRONG_PASSWORD, is_staff=True,
                                        insurance_company=self.mosaic)
        user.groups.add(Group.objects.get(name=group))
        self.client.force_login(user)
        return user

    def test_button_only_for_local(self):
        card, _ = self.make()
        url = f"/admin/counterparties/counterparty/{card.pk}/change/"
        self.assertContains(self.client.get(url), "Сверить с провайдером ЕРСП")
        foreign, _ = self.make(inn="KZ1234567890", is_foreign=True, country="KZ", oked_id=None)
        self.assertNotContains(
            self.client.get(f"/admin/counterparties/counterparty/{foreign.pk}/change/"),
            "Сверить с провайдером ЕРСП",
        )
        self.assertContains(self.client.get("/admin/counterparties/counterparty/"), "Новый по ИНН")

    def test_compare_and_apply(self):
        card = self.empty_card()
        self.answer()
        url = f"/admin/counterparties/counterparty/{card.pk}/provider/"
        response = self.client.get(url)
        exchange = Exchange.objects.get(operation=Exchange.Operation.PROVIDER_INN)
        self.assertRedirects(response, f"{url}?exchange={exchange.pk}")
        self.assertEqual(exchange.subject, INN)
        self.assertEqual(exchange.user, self.bekzod)

        page = self.client.get(f"{url}?exchange={exchange.pk}")
        self.assertContains(page, "23047346")
        self.assertContains(page, f"журнал обмена № {exchange.pk}")
        self.client.get(f"{url}?exchange={exchange.pk}")  # обновление страницы
        self.assertEqual(len(self.fake.calls), 2)        # в ЕРСП не ушло ничего нового

        response = self.client.post(url, {"exchange": exchange.pk, "apply": ["okpo", "director"]})
        self.assertRedirects(response, f"/admin/counterparties/counterparty/{card.pk}/change/",
                             fetch_redirect_response=False)
        self.assertEqual(LegalEntity.objects.get(pk=card.pk).okpo, "23047346")
        self.assertEqual(card.representatives.get().full_name, DIRECTOR)
        self.assertEqual(LegalEntity.objects.get(pk=card.pk).oked_id, None)  # не отмечали

    def test_nothing_to_apply(self):
        card, _ = self.make(counterparty_fields={
            "phone": "998998944440", "address": "Labzak MFY, Zulfiyaxonim ko'chasi, 12-uy",
        })
        Representative(counterparty=card, full_name=DIRECTOR, is_primary=True).save()
        BankAccount(counterparty=card, bank=Bank.objects.create(mfo="01095", name="AAB"),
                    number=ACCOUNT, is_primary=True).save()
        self.answer()
        page = self.client.get(f"/admin/counterparties/counterparty/{card.pk}/provider/", follow=True)
        self.assertContains(page, "совпадает с провайдером - применять нечего")
        self.assertNotContains(page, "Применить отмеченное")
        self.assertContains(page, f"{ACCOUNT} (МФО 01095)", count=2)  # у нас и у провайдера - одинаково

    def test_other_company_answer_not_accepted(self):
        card = self.empty_card()
        stranger = Exchange.objects.create(
            insurance_company=self.other_company, environment="prod",
            operation=Exchange.Operation.PROVIDER_INN, subject=INN, method="POST", url="x",
            outcome=Exchange.Outcome.OK, response_body={"error": 0, "result": inson()},
        )
        url = f"/admin/counterparties/counterparty/{card.pk}/provider/"
        response = self.client.post(url, {"exchange": stranger.pk, "apply": ["okpo"]})
        self.assertRedirects(response, f"/admin/counterparties/counterparty/{card.pk}/change/",
                             fetch_redirect_response=False)
        self.assertEqual(LegalEntity.objects.get(pk=card.pk).okpo, "")

    def test_ersp_error_shown(self):
        card = self.empty_card()
        self.fake.answers += [token_answer(), (200, {"error": 404, "error_message": "Не найдено"})]
        response = self.client.get(f"/admin/counterparties/counterparty/{card.pk}/provider/", follow=True)
        self.assertContains(response, "Провайдер ЕРСП: ЕРСП: Не найдено")

    def test_foreign_not_queried(self):
        foreign, _ = self.make(inn="KZ1234567890", is_foreign=True, country="KZ", oked_id=None)
        response = self.client.get(f"/admin/counterparties/counterparty/{foreign.pk}/provider/", follow=True)
        self.assertContains(response, "только организации Узбекистана")
        self.assertEqual(self.fake.calls, [])

    def test_employee_view_only_cannot(self):
        card = self.empty_card()
        self.login_staff(EMPLOYEE_GROUP)
        with refusal_expected(self):
            response = self.client.get(f"/admin/counterparties/counterparty/{card.pk}/provider/")
        self.assertEqual(response.status_code, 403)
        with refusal_expected(self):
            self.assertEqual(self.client.get("/admin/counterparties/counterparty/by-inn/").status_code, 403)
        self.assertEqual(self.fake.calls, [])

    def test_reference_admin_may(self):
        card = self.empty_card()
        self.login_staff(ADMIN_GROUP)
        self.answer()
        response = self.client.get(f"/admin/counterparties/counterparty/{card.pk}/provider/", follow=True)
        self.assertContains(response, "23047346")

    def test_other_company_card_not_reachable(self):
        card, _ = self.make(company=self.other_company)
        self.login_staff(ADMIN_GROUP)
        response = self.client.get(f"/admin/counterparties/counterparty/{card.pk}/provider/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.fake.calls, [])

    def test_by_inn_new(self):
        url = "/admin/counterparties/counterparty/by-inn/"
        self.assertContains(self.client.get(url), "Запросить у провайдера")
        page = self.client.post(url, {"action": "query", "inn": "12345"})
        self.assertContains(page, "ровно 9 цифр")
        self.answer()
        response = self.client.post(url, {"action": "query", "inn": " 207 078 596 "})
        exchange = Exchange.objects.get(operation=Exchange.Operation.PROVIDER_INN)
        self.assertRedirects(response, f"{url}?exchange={exchange.pk}")
        preview = self.client.get(f"{url}?exchange={exchange.pk}")
        self.assertContains(preview, "Завести карточку")
        self.assertContains(preview, DIRECTOR)

        response = self.client.post(url, {"exchange": exchange.pk, "action": "create",
                                          "apply": ["short_name", "okpo", "director", "account"]})
        card = Counterparty.objects.get()
        self.assertRedirects(response, f"/admin/counterparties/counterparty/{card.pk}/change/",
                             fetch_redirect_response=False)
        self.assertEqual(card.legal_entity.inn, INN)
        self.assertEqual(card.legal_entity.okpo, "23047346")
        self.assertEqual(card.legal_entity.oked_id, None)  # не отмечали
        self.assertEqual(card.accounts.get().number, ACCOUNT)

    def test_by_inn_answer_without_name(self):
        self.answer(inson(name=""))
        url = "/admin/counterparties/counterparty/by-inn/"
        preview = self.client.post(url, {"action": "query", "inn": INN}, follow=True)
        self.assertContains(preview, "нет наименования организации")
        self.assertNotContains(preview, "Завести карточку")

    def test_by_inn_existing_goes_to_compare(self):
        card, _ = self.make()
        response = self.client.post("/admin/counterparties/counterparty/by-inn/",
                                    {"action": "query", "inn": INN})
        self.assertRedirects(response, f"/admin/counterparties/counterparty/{card.pk}/provider/",
                             fetch_redirect_response=False)
        self.assertEqual(self.fake.calls, [])  # повторный запрос - уже со страницы сверки
