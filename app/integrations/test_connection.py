"""
Автотесты модели «Подключение к ЕРСП» (ErspConnection, ErspCredentials):
карточка и блоки сред, шифрование client_secret и пароля, журнал изменений
без секретов, проверки в базе.

Ключ шифрования - во временной папке вместо /run/secrets.

Запуск на сервере: bin/test integrations
"""
import json

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from accounts.models import User
from audit.models import ChangeLog, UntrackedChangeError
from companies.models import InsuranceCompany
from config.crypto import CryptoError
from config.test_crypto import KeyFolderMixin

from .models import Environment, ErspConnection, ErspCredentials

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
SECRET = "client-secret-VALUE"
PASSWORD = 'pa"ss\\word-VALUE'


class ConnectionTestCase(KeyFolderMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mosaic = InsuranceCompany.objects.create(name="Mosaic Insurance Group", code="mosaic")
        cls.other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")
        cls.bekzod = User.objects.create_user("bekzod", password=STRONG_PASSWORD, insurance_company=cls.mosaic)

    def filled(self, company=None, environment=Environment.PROD):
        """Блок среды с заданными значениями, сохранённый от имени Бекзода."""
        connection_ = ErspConnection.for_company(company or self.mosaic, by=self.bekzod)
        credentials = connection_.credentials.get(environment=environment)
        credentials.client_id = "5_test_client"
        credentials.username = "mosaic_ins"
        credentials.set_secret("client_secret", SECRET)
        credentials.set_secret("password", PASSWORD)
        credentials.save(by=self.bekzod)
        return credentials


class ForCompanyTests(ConnectionTestCase):
    def test_creates_card_and_both_environments(self):
        connection_ = ErspConnection.for_company(self.mosaic, by=self.bekzod)
        self.assertEqual(connection_.send_environment, Environment.SANDBOX)
        self.assertEqual(
            sorted(connection_.credentials.values_list("environment", flat=True)),
            ["prod", "sandbox"],
        )
        self.assertEqual(connection_.created_by, self.bekzod)

    def test_second_call_creates_nothing(self):
        first = ErspConnection.for_company(self.mosaic, by=self.bekzod)
        second = ErspConnection.for_company(self.mosaic, by=self.bekzod)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ErspConnection.objects.count(), 1)
        self.assertEqual(ErspCredentials.objects.count(), 2)

    def test_restores_missing_environment(self):
        connection_ = ErspConnection.for_company(self.mosaic)
        # Блок песочницы пропал (так быть не должно, но карточка не ломается).
        ErspCredentials._base_manager.filter(connection=connection_, environment="sandbox").delete()
        ErspConnection.for_company(self.mosaic)
        self.assertEqual(connection_.credentials.count(), 2)

    def test_each_company_own_card(self):
        ErspConnection.for_company(self.mosaic)
        ErspConnection.for_company(self.other)
        self.assertEqual(ErspConnection.objects.count(), 2)
        self.assertEqual(ErspCredentials.objects.count(), 4)

    def test_records_not_deleted(self):
        connection_ = ErspConnection.for_company(self.mosaic)
        with self.assertRaises(UntrackedChangeError):
            connection_.delete()
        with self.assertRaises(UntrackedChangeError):
            ErspCredentials.objects.all().delete()


class SecretTests(ConnectionTestCase):
    def test_round_trip_after_reload(self):
        credentials = self.filled()
        fresh = ErspCredentials.objects.get(pk=credentials.pk)
        self.assertEqual(fresh.secret("client_secret"), SECRET)
        self.assertEqual(fresh.secret("password"), PASSWORD)
        self.assertEqual(fresh.missing(), [])
        self.assertEqual(
            fresh.login_data(),
            {"client_id": "5_test_client", "client_secret": SECRET, "username": "mosaic_ins", "password": PASSWORD},
        )

    def test_database_has_only_cipher(self):
        credentials = self.filled()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT client_secret_cipher, password_cipher FROM integrations_erspcredentials WHERE id = %s",
                [credentials.pk],
            )
            row = cursor.fetchone()
        for value in row:
            self.assertTrue(value.startswith("v1:"))
            self.assertNotIn(SECRET, value)
            self.assertNotIn(PASSWORD, value)

    def test_empty_means_not_set(self):
        credentials = ErspConnection.for_company(self.mosaic).credentials.get(environment="prod")
        self.assertFalse(credentials.has_secret("password"))
        self.assertEqual(credentials.secret("password"), "")
        self.assertEqual(credentials.missing(), ["client_id", "client_secret", "логин", "пароль"])

    def test_clear_secret(self):
        credentials = self.filled()
        credentials.set_secret("password", "")
        credentials.save(by=self.bekzod)
        self.assertFalse(ErspCredentials.objects.get(pk=credentials.pk).has_secret("password"))

    def test_cipher_from_other_company_not_decrypted(self):
        # Кто-то с доступом к базе скопировал шифр пароля Mosaic в строку другой страховой.
        mosaic = self.filled()
        other = ErspConnection.for_company(self.other).credentials.get(environment="prod")
        ErspCredentials._base_manager.filter(pk=other.pk).update(password_cipher=mosaic.password_cipher)
        with self.assertRaises(CryptoError):
            ErspCredentials.objects.get(pk=other.pk).secret("password")

    def test_cipher_from_other_environment_not_decrypted(self):
        prod = self.filled()
        sandbox = prod.connection.credentials.get(environment="sandbox")
        ErspCredentials._base_manager.filter(pk=sandbox.pk).update(password_cipher=prod.password_cipher)
        with self.assertRaises(CryptoError):
            ErspCredentials.objects.get(pk=sandbox.pk).secret("password")

    def test_values_trimmed(self):
        credentials = ErspConnection.for_company(self.mosaic).credentials.get(environment="prod")
        credentials.client_id = "  5_test_client "
        credentials.username = " mosaic_ins  "
        credentials.save(by=self.bekzod)
        credentials.refresh_from_db()
        self.assertEqual((credentials.client_id, credentials.username), ("5_test_client", "mosaic_ins"))


class JournalTests(ConnectionTestCase):
    def entries(self, field):
        return ChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(ErspCredentials), field=field
        ).order_by("id")

    def test_secret_change_logged_without_value(self):
        credentials = self.filled()
        credentials.set_secret("password", "new-password-VALUE")
        credentials.save(by=self.bekzod)
        credentials.set_secret("password", "")
        credentials.save(by=self.bekzod)
        self.assertEqual(
            [(e.action, e.old_value, e.new_value) for e in self.entries("password_cipher")],
            # задан впервые, сменён, стёрт
            [("update", None, "***"), ("update", "***", "***"), ("update", "***", None)],
        )

    def test_open_fields_logged_as_is(self):
        self.filled()
        self.assertEqual([e.new_value for e in self.entries("client_id")], ["5_test_client"])
        self.assertEqual([e.new_value for e in self.entries("username")], ["mosaic_ins"])

    def test_no_secret_or_cipher_anywhere_in_journal(self):
        credentials = self.filled()
        credentials.set_secret("client_secret", "second-secret-VALUE")
        credentials.save(by=self.bekzod)
        dump = json.dumps(list(ChangeLog.objects.values("old_value", "new_value", "object_repr")), ensure_ascii=False)
        for value in [SECRET, PASSWORD, "second-secret-VALUE", "v1:"]:
            self.assertNotIn(value, dump)

    def test_journal_on_company_and_card(self):
        credentials = self.filled()
        entry = self.entries("password_cipher").last()
        self.assertEqual(entry.user, self.bekzod)
        self.assertEqual(entry.insurance_company, self.mosaic)
        self.assertEqual(entry.root_content_type, ContentType.objects.get_for_model(ErspConnection))
        self.assertEqual(entry.root_object_id, credentials.connection_id)

    def test_send_environment_change_logged(self):
        connection_ = ErspConnection.for_company(self.mosaic, by=self.bekzod)
        connection_.send_environment = Environment.PROD
        connection_.save(by=self.bekzod)
        entry = ChangeLog.objects.filter(field="send_environment", action="update").get()
        self.assertEqual((entry.old_value, entry.new_value, entry.user), ("sandbox", "prod", self.bekzod))


class DatabaseProtectionTests(ConnectionTestCase):
    """Проверки в самой базе: действуют и в обход приложения."""

    def assertRefused(self, **values):
        credentials = ErspConnection.for_company(self.mosaic).credentials.get(environment="prod")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ErspCredentials._base_manager.filter(pk=credentials.pk).update(**values)

    def test_plain_password_refused(self):
        self.assertRefused(password_cipher="pa$$word")

    def test_plain_client_secret_refused(self):
        self.assertRefused(client_secret_cipher="client secret")

    def test_unknown_environment_refused(self):
        self.assertRefused(environment="test")

    def test_second_block_same_environment_refused(self):
        connection_ = ErspConnection.for_company(self.mosaic)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ErspCredentials(connection=connection_, environment="prod").save()

    def test_second_card_same_company_refused(self):
        ErspConnection.for_company(self.mosaic)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ErspConnection(insurance_company=self.mosaic).save()

    def test_unknown_send_environment_refused(self):
        connection_ = ErspConnection.for_company(self.mosaic)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ErspConnection._base_manager.filter(pk=connection_.pk).update(send_environment="test")
