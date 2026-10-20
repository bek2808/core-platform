"""
Автотесты пользователя: новые поля, журнал изменений, проверки в базе, экран.

Главное, что проверяется:
- телефон и ПИНФЛ приводятся к цифрам и проверяются и в коде, и в базе;
- администратор платформы - без страховой; страховая после создания не меняется
  (в коде и триггером в базе);
- журнал: кто завёл, заблокировал, сменил; пароль в журнал не попадает;
- вход пользователя не затирает «кто изменил» и не пишет в журнал;
- массовое изменение и удаление запрещены;
- экран: чужая страховая не видна, права не повысить; проверяется от лица
  обычного сотрудника с правами на пользователей, а не суперпользователя.

Запуск на сервере: bin/test accounts
"""
import re

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from audit.models import ChangeLog, UntrackedChangeError
from companies.models import InsuranceCompany
from config.testing import refusal_expected

from .models import User

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
LIST_URL = "/admin/accounts/user/"
ADD_URL = f"{LIST_URL}add/"


class UserCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mosaic = InsuranceCompany.objects.create(name="Mosaic", code="mosaic", country="KZ")
        cls.other = InsuranceCompany.objects.create(name="Other", code="other", country="KZ")
        cls.root = User.objects.create_superuser("root", password=STRONG_PASSWORD)

    def make(self, username="ivanov", company="mosaic", by=None, **fields):
        company = {"mosaic": self.mosaic, "other": self.other, None: None}[company]
        user = User(username=username, insurance_company=company, **fields)
        user.set_password(STRONG_PASSWORD)
        user.full_clean()
        user.save(by=by)
        return user

    def errors(self, user):
        with self.assertRaises(ValidationError) as caught:
            user.full_clean()
        return caught.exception.message_dict


class FieldsTests(UserCase):
    def test_defaults(self):
        user = self.make()
        self.assertEqual(user.kind, User.Kind.EMPLOYEE)
        self.assertEqual((user.patronymic, user.phone, user.pinfl), ("", "", ""))

    def test_phone_and_pinfl_normalized(self):
        user = self.make(phone="+998 (90) 123-45-67", pinfl=" 3010 5780 1234 56 ")
        user.refresh_from_db()
        self.assertEqual(user.phone, "998901234567")
        self.assertEqual(user.pinfl, "30105780123456")

    def test_names_squashed(self):
        user = self.make(last_name="  Каримов ", first_name="Акмал  Ботирович", patronymic=" Ботирович ")
        self.assertEqual(user.full_name(), "Каримов Акмал Ботирович Ботирович")

    def test_full_name_falls_back_to_username(self):
        self.assertEqual(self.make().full_name(), "ivanov")

    def test_phone_format(self):
        self.assertIn("phone", self.errors(User(username="a", insurance_company=self.mosaic, phone="123")))

    def test_pinfl_format(self):
        for bad in ["1234", "3010578012345a", "301057801234567"]:
            with self.subTest(pinfl=bad):
                self.assertIn("pinfl", self.errors(User(username="a", insurance_company=self.mosaic, pinfl=bad)))

    def test_database_checks_phone_pinfl_kind(self):
        user = self.make()
        # _base_manager - намеренный обход запретов кода, чтобы проверить базу.
        for changes in ({"phone": "12ab"}, {"pinfl": "123"}, {"kind": "boss"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                User._base_manager.filter(pk=user.pk).update(**changes)


class CompanyRulesTests(UserCase):
    def test_platform_has_no_company(self):
        platform = User(username="platform", kind=User.Kind.PLATFORM, insurance_company=self.mosaic)
        self.assertIn("insurance_company", self.errors(platform))
        self.make("platform", company=None, kind=User.Kind.PLATFORM)

    def test_platform_with_company_refused_by_database(self):
        user = self.make()
        with self.assertRaises(IntegrityError), transaction.atomic():
            User._base_manager.filter(pk=user.pk).update(kind="platform")

    def test_employee_needs_company(self):
        self.assertIn("insurance_company", self.errors(User(username="a")))

    def test_superuser_may_have_no_company(self):
        User(username="technical", is_superuser=True, password="x").full_clean()

    def test_company_cannot_change(self):
        user = self.make()
        user.insurance_company = self.other
        self.assertIn("не меняется", self.errors(user)["insurance_company"][0])

    def test_company_cannot_change_in_database(self):
        user = self.make()
        with self.assertRaises(IntegrityError), transaction.atomic():
            User._base_manager.filter(pk=user.pk).update(insurance_company=self.other)

    def test_company_can_be_set_once_where_it_was_empty(self):
        user = self.make("technical", company=None, is_superuser=True)
        user.insurance_company = self.mosaic
        user.full_clean()
        user.save()
        user.refresh_from_db()
        self.assertEqual(user.insurance_company, self.mosaic)


class JournalTests(UserCase):
    def rows(self, user, **filters):
        return ChangeLog.objects.filter(object_id=user.pk, content_type__model="user", **filters)

    def test_creation_logged_with_author_and_company(self):
        user = self.make(by=self.root)
        row = self.rows(user, field="username").get()
        self.assertEqual((row.user, row.new_value), (self.root, "ivanov"))
        self.assertEqual(row.insurance_company, self.mosaic)
        self.assertEqual(user.created_by, self.root)

    def test_password_never_in_journal(self):
        user = self.make(by=self.root)
        row = self.rows(user, field="password").get()
        self.assertEqual((row.old_value, row.new_value), (None, "***"))
        for entry in self.rows(user):
            self.assertNotIn(STRONG_PASSWORD, str(entry.new_value))
            self.assertNotIn("pbkdf2", str(entry.new_value))

    def test_password_change_logged_without_values(self):
        user = self.make(by=self.root)
        user.set_password("Another#Strong9Pass")
        user.save(by=self.root)
        row = self.rows(user, field="password").order_by("-id").first()
        self.assertEqual((row.old_value, row.new_value), ("***", "***"))

    def test_blocking_logged_with_who(self):
        user = self.make(by=self.root)
        admin_user = self.make("admin2", by=self.root)
        user.is_active = False
        user.save(by=admin_user)
        row = self.rows(user, field="is_active").order_by("-id").first()
        self.assertEqual((row.user, row.old_value, row.new_value), (admin_user, True, False))
        user.refresh_from_db()
        self.assertEqual(user.updated_by, admin_user)

    def test_login_neither_logs_nor_overwrites_author(self):
        user = self.make(by=self.root)
        before = ChangeLog.objects.count()
        self.assertTrue(self.client.login(username="ivanov", password=STRONG_PASSWORD))
        user.refresh_from_db()
        self.assertIsNotNone(user.last_login)
        self.assertEqual(user.updated_by, self.root)  # вход не затёр «кто изменил»
        self.assertEqual(ChangeLog.objects.count(), before)

    def test_service_save_by_person_is_still_logged(self):
        # Служебным считается только сохранение «от системы»: если поле
        # правит человек, это попадает в журнал как обычно.
        user = self.make(by=self.root)
        user.is_active = False
        user.save(by=self.root, update_fields=["is_active"])
        self.assertTrue(self.rows(user, field="is_active").exists())

    def test_other_fields_service_save_is_logged(self):
        user = self.make(by=self.root)
        user.is_active = False
        user.save(update_fields=["is_active", "last_login"])  # не только служебные поля
        self.assertTrue(self.rows(user, field="is_active", source="system").exists())


class ProtectionTests(UserCase):
    def test_no_bulk_change(self):
        self.make()
        with self.assertRaises(UntrackedChangeError):
            User.objects.filter(username="ivanov").update(is_active=False)

    def test_no_delete(self):
        with self.assertRaises(UntrackedChangeError):
            self.make().delete()
        with self.assertRaises(UntrackedChangeError):
            User.objects.filter(username="ivanov").delete()


def user_with_permissions(company, username="company_admin", *codenames):
    user = User.objects.create_user(username, password=STRONG_PASSWORD, is_staff=True, insurance_company=company)
    user.user_permissions.set(Permission.objects.filter(content_type__app_label="accounts", codename__in=codenames))
    return user


class UserAdminTests(UserCase):
    """Экран «Пользователи» от лица обычных сотрудников, не суперпользователя."""

    def login_company_admin(self, *codenames, company=None):
        codenames = codenames or ("view_user", "add_user", "change_user")
        user = user_with_permissions(company or self.mosaic, "company_admin", *codenames)
        self.client.force_login(user)
        return user

    def new_user_data(self, **extra):
        return {
            "username": "karimov", "usable_password": "true",
            "password1": STRONG_PASSWORD, "password2": STRONG_PASSWORD,
            "last_name": "Каримов", "first_name": "Акмал", "patronymic": "Ботирович",
            "email": "", "phone": "+998 90 123-45-67", "pinfl": "", "kind": "employee", **extra,
        }

    def test_list_shows_only_own_company(self):
        self.make("own_user")
        self.make("foreign_user", company="other")
        self.login_company_admin()
        page = self.client.get(LIST_URL)
        self.assertContains(page, "own_user")
        self.assertNotContains(page, "foreign_user")

    def test_foreign_user_not_opened(self):
        stranger = self.make("foreign_user", company="other")
        self.login_company_admin()
        page = self.client.get(f"{LIST_URL}{stranger.pk}/change/")
        self.assertEqual(page.status_code, 302)  # Django: «не найдено» -> на главную админ-панели
        self.assertNotContains(self.client.get(LIST_URL), "foreign_user")

    def test_create_gets_own_company_and_is_logged(self):
        admin_user = self.login_company_admin()
        page = self.client.post(ADD_URL, self.new_user_data())
        self.assertEqual(page.status_code, 302, re.findall(r'errorlist.*?</ul>', page.content.decode(), re.S))
        user = User.objects.get(username="karimov")
        self.assertEqual(user.insurance_company, self.mosaic)
        self.assertEqual((user.phone, user.created_by), ("998901234567", admin_user))
        self.assertTrue(user.check_password(STRONG_PASSWORD))
        self.assertTrue(ChangeLog.objects.filter(object_id=user.pk, field="phone", user=admin_user).exists())

    def test_foreign_company_in_post_ignored(self):
        self.login_company_admin()
        page = self.client.post(ADD_URL, self.new_user_data(insurance_company=str(self.other.pk)))
        self.assertEqual(page.status_code, 302, page.content.decode()[:3000])
        self.assertEqual(User.objects.get(username="karimov").insurance_company, self.mosaic)

    def test_add_form_hides_company_and_privileges_and_platform_kind(self):
        self.login_company_admin()
        page = self.client.get(ADD_URL).content.decode()
        self.assertNotIn('name="insurance_company"', page)
        self.assertNotIn('value="platform"', page)
        self.assertIn('value="agent"', page)

    def test_platform_kind_refused_in_post(self):
        self.login_company_admin()
        page = self.client.post(ADD_URL, self.new_user_data(kind="platform"))
        self.assertEqual(page.status_code, 200)  # снова форма, не 500
        self.assertFalse(User.objects.filter(username="karimov").exists())

    def test_wrong_phone_shows_message(self):
        self.login_company_admin()
        page = self.client.post(ADD_URL, self.new_user_data(phone="123"))
        self.assertContains(page, "Телефон - от 7 до 15 цифр")

    def test_cannot_raise_own_rights(self):
        admin_user = self.login_company_admin()
        page = self.client.get(f"{LIST_URL}{admin_user.pk}/change/").content.decode()
        for name in ("is_superuser", "groups", "user_permissions"):
            self.assertNotIn(f'name="{name}"', page)
        self.client.post(f"{LIST_URL}{admin_user.pk}/change/", {
            "username": "company_admin", "is_active": "on", "is_staff": "on",
            "is_superuser": "on", "kind": "employee", "date_joined_0": "2026-10-01", "date_joined_1": "10:00:00",
        })
        admin_user.refresh_from_db()
        self.assertFalse(admin_user.is_superuser)

    def test_company_cannot_be_changed_in_post(self):
        user = self.make("own_user")
        self.login_company_admin()
        self.client.post(f"{LIST_URL}{user.pk}/change/", {
            "username": "own_user", "is_active": "on", "kind": "employee",
            "insurance_company": str(self.other.pk),
        })
        user.refresh_from_db()
        self.assertEqual(user.insurance_company, self.mosaic)

    def test_block_through_screen_is_logged(self):
        user = self.make("own_user")
        admin_user = self.login_company_admin()
        page = self.client.post(f"{LIST_URL}{user.pk}/change/", {
            "username": "own_user", "kind": "employee",  # «активен» не отмечен - блокировка
        })
        self.assertEqual(page.status_code, 302, page.content.decode()[:3000])
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        row = ChangeLog.objects.filter(object_id=user.pk, field="is_active").order_by("-id").first()
        self.assertEqual((row.user, row.new_value), (admin_user, False))

    def test_no_delete(self):
        user = self.make("own_user")
        self.login_company_admin("view_user", "delete_user")
        with refusal_expected(self):
            page = self.client.get(f"{LIST_URL}{user.pk}/delete/")
        self.assertEqual(page.status_code, 403)

    def test_user_without_company_sees_nothing(self):
        # Будущий администратор платформы: без страховой и не суперпользователь.
        self.make("own_user")
        platform = User.objects.create_user(
            "platform", password=STRONG_PASSWORD, is_staff=True, kind=User.Kind.PLATFORM,
        )
        platform.user_permissions.set(Permission.objects.filter(content_type__app_label="accounts", codename="view_user"))
        self.client.force_login(platform)
        self.assertNotContains(self.client.get(LIST_URL), "own_user")

    def test_superuser_sees_all_and_chooses_company(self):
        self.make("foreign_user", company="other")
        self.client.force_login(self.root)
        self.assertContains(self.client.get(LIST_URL), "foreign_user")
        self.assertContains(self.client.get(ADD_URL), 'name="insurance_company"')
        page = self.client.post(ADD_URL, self.new_user_data(insurance_company=str(self.other.pk)))
        self.assertEqual(page.status_code, 302, page.content.decode()[:3000])
        self.assertEqual(User.objects.get(username="karimov").insurance_company, self.other)

    def test_history_button_leads_to_journal(self):
        user = self.make("own_user", by=self.root)
        self.client.force_login(self.root)
        page = self.client.get(f"{LIST_URL}{user.pk}/history/")
        self.assertEqual(page.status_code, 302)
        self.assertIn("card=", page["Location"])
