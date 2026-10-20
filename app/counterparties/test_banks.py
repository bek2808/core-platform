"""
Автотесты связи нашего справочника банков с перечнем ЦБ (counterparties/banks.py,
команда load_cbu_banks, экран банка, новый банк от провайдера).

Правило (решения Бекзода 21.10): ЦБ заполняет пустой ИНН и название-заглушку
«Банк <МФО>»; то, что ввели люди, не трогает; расхождения ИНН и банки,
которых нет в перечне ЦБ, только показываются.

В сеть тесты не ходят: файл ЦБ собирается в тесте (references.test_cbu.cbu_xlsx).

Запуск на сервере: bin/test counterparties
"""
import io
import tempfile
from pathlib import Path

from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command

from accounts.models import User
from accounts.testing import give_roles
from audit.models import ChangeLog, Source
from references import cbu
from references.models import CbuBank
from references.test_cbu import ALOQABANK, ASIA_ALLIANCE, KAPITALBANK, FakeFetch, cbu_xlsx

from . import banks, provider
from .models import Bank
from .test_provider import INN, inson
from .tests import ADMIN_GROUP, STRONG_PASSWORD, CounterpartyTestCase


def load(rows=None):
    cbu.sync(cbu.read_rows(cbu_xlsx(rows) if rows is not None else cbu_xlsx()))


class BankCase(CounterpartyTestCase):
    def journal(self, bank):
        return ChangeLog.objects.filter(content_type=ContentType.objects.get_for_model(Bank), object_id=bank.pk)

    def bank(self, mfo, name, inn=""):
        bank = Bank(mfo=mfo, name=name, inn=inn)
        bank.save(by=self.bekzod)
        return bank


class FillTests(BankCase):
    def test_empty_inn_filled_typed_name_kept(self):
        load()
        bank = self.bank("00401", "Алокабанк (наше название)")
        report = banks.fill_all_from_cbu()
        bank.refresh_from_db()
        self.assertEqual(bank.inn, "200829053")
        self.assertEqual(bank.name, "Алокабанк (наше название)")
        self.assertEqual((report.inn_filled, report.name_filled), (["00401"], []))
        entry = self.journal(bank).get(field="inn", action="update")
        self.assertEqual((entry.source, entry.user, entry.new_value), (Source.CBU, None, "200829053"))

    def test_placeholder_name_replaced(self):
        load()
        bank = self.bank("01095", banks.placeholder_name("01095"))
        report = banks.fill_all_from_cbu()
        bank.refresh_from_db()
        self.assertEqual(bank.name, '"ASIA ALLIANCE BANK" AT BANKI')
        self.assertEqual(report.name_filled, ["01095"])

    def test_different_inn_reported_not_changed(self):
        load()
        bank = self.bank("00401", "Алокабанк", inn="111111111")
        report = banks.fill_all_from_cbu()
        bank.refresh_from_db()
        self.assertEqual(bank.inn, "111111111")
        self.assertEqual(report.inn_differs, [("00401", "111111111", "200829053")])
        self.assertFalse(self.journal(bank).filter(source=Source.CBU).exists())

    def test_not_in_cbu_reported_not_deactivated(self):
        load()
        bank = self.bank("00842", "KDB")
        report = banks.fill_all_from_cbu()
        bank.refresh_from_db()
        self.assertTrue(bank.is_active)
        self.assertEqual(report.not_in_cbu, ["00842"])

    def test_gone_from_cbu_not_used(self):
        load()
        load([ASIA_ALLIANCE])  # Алокабанк пропал из нового файла
        bank = self.bank("00401", "Алокабанк")
        report = banks.fill_all_from_cbu()
        bank.refresh_from_db()
        self.assertEqual(bank.inn, "")
        self.assertEqual(report.not_in_cbu, ["00401"])

    def test_second_run_changes_nothing(self):
        load()
        self.bank("00401", "Алокабанк")
        banks.fill_all_from_cbu()
        count = ChangeLog.objects.count()
        report = banks.fill_all_from_cbu()
        self.assertEqual((report.inn_filled, report.name_filled), ([], []))
        self.assertEqual(ChangeLog.objects.count(), count)


class CommandTests(BankCase):
    def run_command(self, *args, fetch=None):
        original = cbu.urllib_fetch
        cbu.urllib_fetch = fetch or FakeFetch()  # вместо сайта ЦБ - заглушка
        self.addCleanup(setattr, cbu, "urllib_fetch", original)
        out = io.StringIO()
        call_command("load_cbu_banks", *args, stdout=out)
        return out.getvalue()

    def test_download_and_fill(self):
        self.bank("00401", "Алокабанк")
        self.bank("00842", "KDB", inn="200000001")
        output = self.run_command()
        self.assertIn("Filiallar_haqida_malumot.xlsx", output)
        self.assertIn("Строк с банками: 4 (головной офис банка - 2, платёжный центр банка - 1, филиал банка - 1)", output)
        self.assertIn("Перечень ЦБ: новых 4, изменено 0, без изменений 0, пропало из перечня 0.", output)
        self.assertIn("ИНН дописан из ЦБ - 1 (00401)", output)
        self.assertIn("Нет в перечне ЦБ (банк не выключали): МФО 00842", output)
        self.assertEqual(Bank.objects.get(mfo="00401").inn, "200829053")
        self.assertEqual(CbuBank.objects.count(), 4)

    def test_second_run_unchanged(self):
        self.run_command()
        output = self.run_command()
        self.assertIn("новых 0, изменено 0, без изменений 4", output)

    def test_bad_file_changes_nothing(self):
        self.bank("00401", "Алокабанк")
        bad = FakeFetch(cbu_xlsx([ALOQABANK, {**KAPITALBANK, "ИНН": "123"}]))
        with self.assertRaisesMessage(CommandError, "в базе ничего не изменилось"):
            self.run_command(fetch=bad)
        self.assertFalse(CbuBank.objects.exists())
        self.assertEqual(Bank.objects.get(mfo="00401").inn, "")

    def test_site_unreachable(self):
        def offline(url):
            raise cbu.LoadError(f"не удалось скачать {url}: timed out")
        with self.assertRaisesMessage(CommandError, "timed out"):
            self.run_command(fetch=offline)

    def test_file_option(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "banks.xlsx"
            path.write_bytes(cbu_xlsx())
            fetch = FakeFetch()
            output = self.run_command("--file", str(path), fetch=fetch)
        self.assertEqual(fetch.calls, [])  # сайт не трогали
        self.assertIn("новых 4", output)

    def test_inn_difference_shown(self):
        self.bank("00401", "Алокабанк", inn="111111111")
        output = self.run_command()
        self.assertIn("ИНН отличается от ЦБ (не меняли): МФО 00401 - у нас 111111111, в ЦБ 200829053", output)


class ProviderBankTests(BankCase):
    def test_new_bank_taken_from_cbu(self):
        load()
        card, _ = self.make()
        rows = provider.compare(card, provider.parse(INN, inson()))
        account_row = next(row for row in rows if row.key == provider.ACCOUNT)
        self.assertIn('по перечню ЦБ: "ASIA ALLIANCE BANK" AT BANKI, ИНН 207018693', account_row.note)
        done = provider.apply(card, provider.parse(INN, inson()), ["account"], self.bekzod)
        bank = Bank.objects.get(mfo="01095")
        self.assertEqual((bank.name, bank.inn), ('"ASIA ALLIANCE BANK" AT BANKI', "207018693"))
        self.assertIn("по перечню ЦБ", done[-1])
        self.assertNotIn("допишите", done[-1])
        entry = self.journal(bank).get(field="inn")
        self.assertEqual((entry.source, entry.user), (Source.CBU, self.bekzod))

    def test_not_in_cbu_works_as_before(self):
        load([ALOQABANK])
        card, _ = self.make()
        done = provider.apply(card, provider.parse(INN, inson()), ["account"], self.bekzod)
        bank = Bank.objects.get(mfo="01095")
        self.assertEqual(bank.name, 'ТОШКЕНТ Ш., "ASIA ALLIANCE BANK" АТ БАНКИ')
        self.assertEqual(bank.inn, "")
        self.assertIn("допишите его ИНН", done[-1])

    def test_no_name_from_provider_gives_placeholder(self):
        card, _ = self.make()
        provider.apply(card, provider.parse(INN, inson(bankName="", bank_name="")), ["account"], self.bekzod)
        bank = Bank.objects.get(mfo="01095")
        self.assertEqual(bank.name, "Банк 01095")
        load()
        banks.fill_all_from_cbu()
        bank.refresh_from_db()
        self.assertEqual((bank.name, bank.inn), ('"ASIA ALLIANCE BANK" AT BANKI', "207018693"))


class BankScreenTests(BankCase):
    def login(self):
        user = User.objects.create_user("ivanov", password=STRONG_PASSWORD, is_staff=True,
                                        insurance_company=self.mosaic)
        give_roles(user, ADMIN_GROUP)
        self.client.force_login(user)
        return user

    def test_manual_bank_gets_inn_from_cbu(self):
        load()
        user = self.login()
        response = self.client.post("/admin/counterparties/bank/add/",
                                    {"mfo": "01158", "name": "Капиталбанк", "inn": "", "is_active": "on"},
                                    follow=True)
        self.assertContains(response, "Из перечня ЦБ подставлено: ИНН 207275139.")
        bank = Bank.objects.get(mfo="01158")
        self.assertEqual((bank.name, bank.inn), ("Капиталбанк", "207275139"))
        inn_entries = self.journal(bank).filter(field="inn")
        self.assertEqual([(e.source, e.user) for e in inn_entries], [(Source.CBU, user)])

    def test_typed_inn_kept(self):
        load()
        self.login()
        self.client.post("/admin/counterparties/bank/add/",
                         {"mfo": "01158", "name": "Капиталбанк", "inn": "111111111", "is_active": "on"})
        self.assertEqual(Bank.objects.get(mfo="01158").inn, "111111111")

    def test_card_shows_cbu_data(self):
        load()
        bank = self.bank("00401", "Алокабанк", inn="111111111")
        self.login()
        response = self.client.get(f"/admin/counterparties/bank/{bank.pk}/change/")
        self.assertContains(response, "По данным ЦБ")  # подпись поля, Django пишет с заглавной
        self.assertContains(response, "BOSH OFISI")
        self.assertContains(response, "ИНН 200829053")
        self.assertContains(response, "ИНН в ЦБ отличается от нашего")

    def test_card_when_list_not_loaded(self):
        bank = self.bank("00401", "Алокабанк")
        self.login()
        self.assertContains(self.client.get(f"/admin/counterparties/bank/{bank.pk}/change/"),
                            "перечень ЦБ ещё не загружен")
