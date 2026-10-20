"""
Автотесты аудита: кто и когда создал и изменил запись.

Своих таблиц у audit пока нет, поэтому проверяем на банке
(counterparties.Bank построен на audit.TrackedModel).

Запуск на сервере: bin/test audit
"""
from django.test import TestCase

from accounts.models import User
from counterparties.models import Bank

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"


class TrackedModelTests(TestCase):
    def setUp(self):
        self.bekzod = User.objects.create_user("bekzod", password=STRONG_PASSWORD)
        self.ivanov = User.objects.create_user("ivanov", password=STRONG_PASSWORD)

    def test_creator_and_editor_recorded(self):
        bank = Bank(mfo="01095", name="Asia Alliance Bank")
        bank.save(by=self.bekzod)
        bank.refresh_from_db()
        self.assertEqual(bank.created_by, self.bekzod)
        self.assertEqual(bank.updated_by, self.bekzod)
        self.assertIsNotNone(bank.created_at)

    def test_second_editor_does_not_replace_creator(self):
        bank = Bank(mfo="01095", name="Asia Alliance Bank")
        bank.save(by=self.bekzod)
        first_change = bank.updated_at

        bank.inn = "123456789"
        bank.save(by=self.ivanov)
        bank.refresh_from_db()
        self.assertEqual(bank.created_by, self.bekzod)
        self.assertEqual(bank.updated_by, self.ivanov)
        self.assertGreater(bank.updated_at, first_change)

    def test_without_user_means_system(self):
        bank = Bank(mfo="01095", name="Asia Alliance Bank")
        bank.save()
        bank.refresh_from_db()
        self.assertIsNone(bank.created_by)
        self.assertIsNone(bank.updated_by)

    def test_partial_save_still_records_editor(self):
        # Сохранение отдельных полей (update_fields) тоже отмечает, кто изменил.
        bank = Bank(mfo="01095", name="Asia Alliance Bank")
        bank.save(by=self.bekzod)
        bank.is_active = False
        bank.save(by=self.ivanov, update_fields=["is_active"])
        bank.refresh_from_db()
        self.assertFalse(bank.is_active)
        self.assertEqual(bank.updated_by, self.ivanov)
