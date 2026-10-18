"""
Автотесты пользователей Core-системы: модель пользователя, правила паролей,
вход в админ-панель.

Запуск на сервере: bin/test accounts (все тесты - bin/test).
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import (
    CommonPasswordValidator,
    get_default_password_validators,
    validate_password,
)
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from .models import User

# Надёжный пароль для тестов: 15 символов, буквы, цифры, знаки.
STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"


class UserModelTests(TestCase):
    """Модель пользователя accounts.User."""

    def test_project_uses_own_user_model(self):
        # Везде в системе пользователь - наша модель, а не стандартная auth.User.
        self.assertIs(get_user_model(), User)

    def test_password_stored_as_hash(self):
        user = User.objects.create_user("ivanov", password=STRONG_PASSWORD)
        user.refresh_from_db()
        # В базе лежит только отпечаток (хеш), сам пароль восстановить нельзя.
        self.assertNotIn(STRONG_PASSWORD, user.password)
        self.assertTrue(user.check_password(STRONG_PASSWORD))
        self.assertFalse(user.check_password("wrong-password-123"))

    def test_new_user_has_no_admin_rights(self):
        # Обычный сотрудник по умолчанию не попадает в админ-панель.
        user = User.objects.create_user("ivanov", password=STRONG_PASSWORD)
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)


class PasswordRulesTests(SimpleTestCase):
    """Правила паролей сотрудников (AUTH_PASSWORD_VALIDATORS в settings.py)."""

    def rejection_codes(self, password, user=None):
        """Коды причин, по которым пароль отклонён; пустой список - пароль принят."""
        try:
            validate_password(password, user)
        except ValidationError as error:
            return [item.code for item in error.error_list]
        return []

    def test_strong_password_accepted(self):
        self.assertEqual(self.rejection_codes(STRONG_PASSWORD), [])

    def test_minimum_length_is_12(self):
        self.assertIn("password_too_short", self.rejection_codes("Kx7#mQ2v!pL"))  # 11
        self.assertEqual(self.rejection_codes("Kx7#mQ2v!pLr"), [])                 # 12

    def test_only_digits_rejected(self):
        self.assertIn("password_entirely_numeric", self.rejection_codes("582049137760"))

    def test_common_password_rejected(self):
        # Берём из встроенного списка Django (20 000 частых паролей) первый,
        # который длиннее 11 символов и не из одних цифр.
        validator = next(
            v for v in get_default_password_validators()
            if isinstance(v, CommonPasswordValidator)
        )
        common = next(
            p for p in sorted(validator.passwords) if len(p) >= 12 and not p.isdigit()
        )
        self.assertIn("password_too_common", self.rejection_codes(common))

    def test_password_similar_to_name_rejected(self):
        user = User(
            username="ivanov.akmal",
            first_name="Akmal",
            last_name="Ivanov",
            email="akmal.ivanov@example.uz",
        )
        self.assertIn("password_too_similar", self.rejection_codes("ivanov.akmal1", user))


class AdminAccessTests(TestCase):
    """Вход в админ-панель."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            "admin_test", "admin@example.uz", STRONG_PASSWORD
        )
        cls.clerk = User.objects.create_user("clerk_test", password=STRONG_PASSWORD)

    def login(self, username, password):
        return self.client.post(
            "/admin/login/?next=/admin/",
            {"username": username, "password": password},
        )

    def is_logged_in(self):
        return "_auth_user_id" in self.client.session

    def test_anonymous_redirected_to_login(self):
        response = self.client.get("/admin/")
        self.assertRedirects(response, "/admin/login/?next=/admin/")

    def test_login_page_shows_our_title(self):
        response = self.client.get("/admin/login/")
        self.assertContains(response, "Core - страховая система")

    def test_admin_can_log_in(self):
        response = self.login("admin_test", STRONG_PASSWORD)
        self.assertRedirects(response, "/admin/")
        self.assertTrue(self.is_logged_in())

    def test_wrong_password_rejected(self):
        response = self.login("admin_test", "wrong-password-123")
        self.assertEqual(response.status_code, 200)   # снова форма входа
        self.assertFalse(self.is_logged_in())

    def test_regular_user_cannot_enter_admin(self):
        # Пароль верный, но у сотрудника нет флага is_staff - в админ-панель не пускаем.
        response = self.login("clerk_test", STRONG_PASSWORD)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.is_logged_in())

    def test_admin_sees_user_list(self):
        self.client.force_login(self.admin)
        response = self.client.get("/admin/accounts/user/")
        self.assertContains(response, "clerk_test")
