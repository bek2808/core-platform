"""
Автотесты справочника контрагентов.

Сейчас: банки, основания полномочий, должности - формат, нормализация,
защита в базе, стартовые списки, админ-панель.

Запуск на сервере: bin/test counterparties
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from accounts.models import User

from .models import Bank, PowerBasis, Position
from .normalize import remove_spaces, squash_spaces

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
        with self.assertRaises(IntegrityError), transaction.atomic():
            Bank.objects.bulk_create([Bank(mfo="ABCDE", name="Банк")])

    def test_database_rejects_wrong_inn(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Bank.objects.bulk_create([Bank(mfo="01095", name="Банк", inn="12345")])


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

    def test_other_flag_not_editable(self):
        # «Иное» нельзя назначить из админ-панели: поле только для чтения.
        self.client.post(
            "/admin/counterparties/position/add/",
            {"name": "Ректор", "is_other": "on", "sort_order": "100", "is_active": "on"},
        )
        self.assertFalse(Position.objects.get(name="Ректор").is_other)
