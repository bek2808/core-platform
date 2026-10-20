"""
Автотесты аудита: кто и когда создал и изменил запись, журнал изменений
(было -> стало), запрет обхода журнала, защита журнала в базе.

Своих «рабочих» таблиц у audit нет, поэтому проверяем на банке
(counterparties.Bank построен на audit.TrackedModel).

Запуск на сервере: bin/test audit
"""
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from accounts.models import User
from counterparties.models import Bank

from .models import ChangeLog, Source, UntrackedChangeError

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"


def journal(bank):
    """Строки журнала по банку: {поле: (было, стало)} последнего сохранения."""
    entries = ChangeLog.objects.filter(
        content_type=ContentType.objects.get_for_model(Bank), object_id=bank.pk
    )
    last = entries.first()
    if last is None:
        return {}
    return {e.field: (e.old_value, e.new_value) for e in entries.filter(change_id=last.change_id)}


class TrackedModelTests(TestCase):
    """Поля «кто и когда» в самой записи."""

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


class JournalTests(TestCase):
    """Журнал изменений: что попадает, с какими значениями, от кого."""

    def setUp(self):
        self.bekzod = User.objects.create_user("bekzod", password=STRONG_PASSWORD)
        self.ivanov = User.objects.create_user("ivanov", password=STRONG_PASSWORD)
        self.bank = Bank(mfo="01095", name="Asia Alliance Bank")
        self.bank.save(by=self.bekzod)

    def test_creation_logged_with_filled_fields_only(self):
        # ИНН пустой - в журнал не попал; служебные поля «кто и когда» тоже нет.
        self.assertEqual(
            journal(self.bank),
            {
                "mfo": (None, "01095"),
                "name": (None, "Asia Alliance Bank"),
                "is_active": (None, True),
            },
        )
        entry = ChangeLog.objects.first()
        self.assertEqual(entry.action, ChangeLog.Action.CREATE)
        self.assertEqual(entry.user, self.bekzod)
        self.assertEqual(entry.source, Source.MANUAL)
        self.assertEqual(entry.object_repr, "01095 - Asia Alliance Bank")
        self.assertIsNone(entry.insurance_company)  # банк - общий справочник

    def test_change_logged_old_to_new(self):
        self.bank.inn = "123456789"
        self.bank.name = "ASIA ALLIANCE BANK AT BANKI"
        self.bank.save(by=self.ivanov)
        self.assertEqual(
            journal(self.bank),
            {
                "inn": ("", "123456789"),
                "name": ("Asia Alliance Bank", "ASIA ALLIANCE BANK AT BANKI"),
            },
        )
        entry = ChangeLog.objects.first()
        self.assertEqual(entry.action, ChangeLog.Action.UPDATE)
        self.assertEqual(entry.user, self.ivanov)

    def test_one_save_one_change_id(self):
        self.bank.inn = "123456789"
        self.bank.is_active = False
        self.bank.save(by=self.ivanov)
        last = ChangeLog.objects.first()
        self.assertEqual(ChangeLog.objects.filter(change_id=last.change_id).count(), 2)

    def test_save_without_changes_writes_nothing(self):
        before = ChangeLog.objects.count()
        self.bank.save(by=self.ivanov)
        self.assertEqual(ChangeLog.objects.count(), before)

    def test_spaces_are_not_a_change(self):
        # « 01095 » после нормализации - то же «01095»: изменения нет.
        before = ChangeLog.objects.count()
        self.bank.mfo = " 01095 "
        self.bank.save(by=self.ivanov)
        self.assertEqual(ChangeLog.objects.count(), before)

    def test_system_change(self):
        self.bank.is_active = False
        self.bank.save()
        entry = ChangeLog.objects.first()
        self.assertIsNone(entry.user)
        self.assertEqual(entry.source, Source.SYSTEM)

    def test_provider_change(self):
        self.bank.name = 'ТОШКЕНТ Ш., "ASIA ALLIANCE BANK" АТ БАНКИ'
        self.bank.save(by=self.ivanov, source=Source.PROVIDER)
        entry = ChangeLog.objects.first()
        self.assertEqual(entry.source, Source.PROVIDER)
        self.assertEqual(entry.user, self.ivanov)

    def test_partial_save_logs_only_saved_fields(self):
        # Сохраняется только «действует»: изменение имени не дошло до базы,
        # значит, и в журнал не попадает.
        self.bank.is_active = False
        self.bank.name = "Не сохранится"
        self.bank.save(by=self.ivanov, update_fields=["is_active"])
        self.assertEqual(journal(self.bank), {"is_active": (True, False)})

    def test_failed_save_leaves_no_journal(self):
        # Ошибка при сохранении - нет ни изменения, ни строки в журнале.
        other = Bank(mfo="00842", name="KDB Bank")
        other.save(by=self.bekzod)
        before = ChangeLog.objects.count()
        other.mfo = "01095"  # занят первым банком
        with self.assertRaises(DatabaseError), transaction.atomic():
            other.save(by=self.ivanov)
        self.assertEqual(ChangeLog.objects.count(), before)


class NoBypassTests(TestCase):
    """Изменения в обход сохранения записи (и, значит, журнала) запрещены."""

    def setUp(self):
        self.bank = Bank(mfo="01095", name="Asia Alliance Bank")
        self.bank.save()

    def test_mass_update_forbidden(self):
        with self.assertRaises(UntrackedChangeError):
            Bank.objects.filter(pk=self.bank.pk).update(name="Тихо")
        self.bank.refresh_from_db()
        self.assertEqual(self.bank.name, "Asia Alliance Bank")

    def test_mass_create_forbidden(self):
        with self.assertRaises(UntrackedChangeError):
            Bank.objects.bulk_create([Bank(mfo="00842", name="KDB Bank")])

    def test_mass_bulk_update_forbidden(self):
        self.bank.name = "Тихо"
        with self.assertRaises(UntrackedChangeError):
            Bank.objects.bulk_update([self.bank], ["name"])

    def test_delete_forbidden(self):
        with self.assertRaises(UntrackedChangeError):
            Bank.objects.filter(pk=self.bank.pk).delete()
        with self.assertRaises(UntrackedChangeError):
            self.bank.delete()
        self.assertTrue(Bank.objects.filter(pk=self.bank.pk).exists())


class AppendOnlyTests(TestCase):
    """Журнал только пополняется: базу защищает триггер (миграция audit/0001)."""

    def setUp(self):
        Bank(mfo="01095", name="Asia Alliance Bank").save()
        self.entry = ChangeLog.objects.first()

    def test_entry_cannot_be_changed(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            ChangeLog.objects.filter(pk=self.entry.pk).update(new_value="подделка")

    def test_entry_cannot_be_deleted(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            ChangeLog.objects.filter(pk=self.entry.pk).delete()

    def test_raw_sql_also_blocked(self):
        # Даже прямой SQL под ролью приложения - отказ.
        for sql in ["UPDATE audit_changelog SET field = 'x'", "DELETE FROM audit_changelog",
                    "TRUNCATE audit_changelog"]:
            with self.subTest(sql=sql), self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql)
        self.assertTrue(ChangeLog.objects.filter(pk=self.entry.pk).exists())


class JournalAdminTests(TestCase):
    """Журнал в админ-панели: только просмотр; кнопка «История» в карточке."""

    def setUp(self):
        self.admin = User.objects.create_superuser("bekzod", password=STRONG_PASSWORD)
        self.client.force_login(self.admin)
        self.bank = Bank(mfo="01095", name="Asia Alliance Bank")
        self.bank.save(by=self.admin)

    def test_change_through_admin_logged(self):
        self.client.post(
            f"/admin/counterparties/bank/{self.bank.pk}/change/",
            {"mfo": "01095", "name": "Asia Alliance Bank", "inn": "123456789", "is_active": "on"},
        )
        self.assertEqual(journal(self.bank), {"inn": ("", "123456789")})
        self.assertEqual(ChangeLog.objects.first().user, self.admin)

    def test_history_button_shows_record_changes(self):
        self.bank.is_active = False
        self.bank.save(by=self.admin)
        response = self.client.get(
            f"/admin/counterparties/bank/{self.bank.pk}/history/", follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "действует")  # название поля, а не is_active
        self.assertContains(response, "да")
        self.assertContains(response, "нет")

    def test_journal_read_only_even_for_superuser(self):
        entry = ChangeLog.objects.first()
        field_before = entry.field
        self.assertEqual(self.client.get("/admin/audit/changelog/").status_code, 200)
        self.assertEqual(
            self.client.get(f"/admin/audit/changelog/{entry.pk}/change/").status_code, 200
        )
        self.assertEqual(self.client.get("/admin/audit/changelog/add/").status_code, 403)
        self.client.post(f"/admin/audit/changelog/{entry.pk}/change/", {"field": "x"})
        self.client.post(f"/admin/audit/changelog/{entry.pk}/delete/", {"post": "yes"})
        entry.refresh_from_db()
        self.assertEqual(entry.field, field_before)
