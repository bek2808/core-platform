"""
Автотесты экрана «Интеграции -> Подключение к ЕРСП».

Главное, что проверяется:
- client_secret и пароль не попадают в страницу ни значением, ни шифром;
- пустое поле при сохранении - «не менять», «стереть» - стирает;
- смена данных для входа забывает токен этой страховой и среды;
- «Проверить подключение» - вход в ЕРСП и итог на экране;
- переход отправки полисов на боевую - только с подтверждением;
- чужая страховая не видна; права - от лица обычного сотрудника группы.

В сеть тесты не ходят: вместо ЕРСП - заглушка FakeErsp.

Запуск на сервере: bin/test integrations
"""
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from accounts.models import User
from audit.models import ChangeLog
from companies.models import InsuranceCompany
from config.testing import refusal_expected

from . import ersp
from .models import ErspConnection, ErspCredentials, Exchange
from .tests import CREDENTIALS, STRONG_PASSWORD, ErspMixin, FakeErsp, fill_credentials, token_answer

GROUP = "Администратор интеграций"
LIST_URL = "/admin/integrations/erspconnection/"


class ScreenCase(ErspMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")
        fill_credentials(cls.other, values={**CREDENTIALS, "client_id": "kz_client", "username": "kz_ins"})
        cls.connection = ErspConnection.objects.get(insurance_company=cls.mosaic)
        cls.foreign = ErspConnection.objects.get(insurance_company=cls.other)

    def setUp(self):
        super().setUp()
        self.fake = FakeErsp()
        original = ersp.urllib_transport
        ersp.urllib_transport = self.fake  # вместо ЕРСП - заглушка
        self.addCleanup(setattr, ersp, "urllib_transport", original)

    def login(self, group=GROUP, company="mosaic", username="ivanov"):
        user = User.objects.create_user(
            username, password=STRONG_PASSWORD, is_staff=True,
            insurance_company=self.mosaic if company == "mosaic" else company,
        )
        if group:
            user.groups.add(Group.objects.get(name=group))
        self.client.force_login(user)
        return user

    def change_url(self, connection=None):
        return f"{LIST_URL}{(connection or self.connection).pk}/change/"

    def save(self, connection=None, prod=None, sandbox=None):
        """Отправить карточку, как кнопка «Сохранить»; prod/sandbox - что изменить в блоке."""
        connection = connection or self.connection
        rows = list(connection.credentials.order_by("environment"))  # prod, sandbox
        data = {
            "credentials-TOTAL_FORMS": str(len(rows)), "credentials-INITIAL_FORMS": str(len(rows)),
            "credentials-MIN_NUM_FORMS": "0", "credentials-MAX_NUM_FORMS": "2", "_save": "Сохранить",
        }
        for index, (row, changes) in enumerate(zip(rows, [prod or {}, sandbox or {}])):
            prefix = f"credentials-{index}-"
            data.update({
                prefix + "id": str(row.pk), prefix + "connection": str(connection.pk),
                prefix + "client_id": row.client_id, prefix + "username": row.username,
            })
            data.update({prefix + key: value for key, value in changes.items()})
        return self.client.post(self.change_url(connection), data)

    def prod(self, connection=None):
        return (connection or self.connection).credentials.get(environment="prod")


class AccessTests(ScreenCase):
    def test_group_permissions(self):
        codenames = set(Group.objects.get(name=GROUP).permissions.values_list("codename", flat=True))
        self.assertEqual(codenames, {
            "view_erspconnection", "change_erspconnection", "view_erspcredentials",
            "change_erspcredentials", "view_exchange", "view_changelog",
        })

    def test_company_user_goes_straight_to_own_card(self):
        self.login()
        response = self.client.get(LIST_URL)
        self.assertRedirects(response, self.change_url())

    def test_card_created_on_first_open(self):
        new = InsuranceCompany.objects.create(name="New Insurance", code="new", country="KZ")
        user = User.objects.create_user("newadmin", password=STRONG_PASSWORD, is_staff=True, insurance_company=new)
        user.groups.add(Group.objects.get(name=GROUP))
        self.client.force_login(user)
        response = self.client.get(LIST_URL)
        connection = ErspConnection.objects.get(insurance_company=new)
        self.assertRedirects(response, self.change_url(connection))
        self.assertEqual(connection.credentials.count(), 2)
        self.assertEqual(connection.created_by, user)

    def test_foreign_card_not_visible(self):
        self.login()
        response = self.client.get(self.change_url(self.foreign))
        self.assertEqual(response.status_code, 302)  # Django: «не найдено» -> на главную
        self.assertNotContains(self.client.get(self.change_url()), "kz_client")
        with refusal_expected(self):
            response = self.client.post(f"{LIST_URL}{self.foreign.pk}/check/prod/")
        self.assertEqual(response.status_code, 403)
        with refusal_expected(self):
            response = self.client.post(f"{LIST_URL}{self.foreign.pk}/send-environment/",
                                        {"send_environment": "prod", "confirm": "yes"})
        self.assertEqual(response.status_code, 403)
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.send_environment, "sandbox")

    def test_other_groups_have_no_access(self):
        for group in ("Сотрудник", "Администратор справочников"):
            with self.subTest(group=group):
                self.login(group=group, username=f"user-{len(group)}")
                with refusal_expected(self):
                    self.assertEqual(self.client.get(self.change_url()).status_code, 403)

    def test_view_only_user_cannot_check_or_switch(self):
        user = self.login(group=None)
        for codename in ("view_erspconnection", "view_erspcredentials"):
            user.user_permissions.add(Permission.objects.get(codename=codename))
        self.assertEqual(self.client.get(self.change_url()).status_code, 200)
        with refusal_expected(self):
            self.assertEqual(self.client.post(f"{LIST_URL}{self.connection.pk}/check/prod/").status_code, 403)
        with refusal_expected(self):
            self.assertEqual(self.client.get(f"{LIST_URL}{self.connection.pk}/send-environment/").status_code, 403)
        self.assertEqual(self.fake.calls, [])

    def test_system_admin_sees_all_companies(self):
        InsuranceCompany.objects.create(name="Third Insurance", code="third", country="KZ")
        admin_user = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        self.client.force_login(admin_user)
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        for name in ("Mosaic Insurance Group", "Test Insurance KZ", "Third Insurance"):
            self.assertContains(response, name)
        self.assertTrue(ErspConnection.objects.filter(insurance_company__code="third").exists())
        self.assertContains(response, "нет: client_id, client_secret, логин, пароль")


class SecretsOnScreenTests(ScreenCase):
    def assertNoSecrets(self, response):
        page = response.content.decode()
        for value in (CREDENTIALS["client_secret"], CREDENTIALS["password"], "v1:"):
            self.assertNotIn(value, page)
        for credentials in ErspCredentials.objects.all():
            self.assertNotIn(credentials.password_cipher, page)
            self.assertNotIn(credentials.client_secret_cipher, page)

    def test_card_shows_open_values_and_hides_secrets(self):
        self.login()
        response = self.client.get(self.change_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "5_test_client")
        self.assertContains(response, "mosaic_ins")
        self.assertContains(response, "задан ••••")
        self.assertNoSecrets(response)

    def test_empty_fields_keep_secrets(self):
        self.login()
        before = self.prod()
        response = self.save(prod={"username": "mosaic_new"})
        # «Сохранить» ведёт к списку, а список сотрудника - снова в его карточку.
        self.assertRedirects(response, LIST_URL, target_status_code=302)
        after = self.prod()
        self.assertEqual(after.username, "mosaic_new")
        self.assertEqual(after.password_cipher, before.password_cipher)
        self.assertEqual(after.secret("password"), CREDENTIALS["password"])

    def test_new_password_saved_encrypted_and_logged(self):
        user = self.login()
        response = self.save(prod={"password": " новый пароль "})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.prod().secret("password"), " новый пароль ")  # пробелы - часть пароля
        entry = ChangeLog.objects.filter(
            content_type=ContentType.objects.get_for_model(ErspCredentials), field="password_cipher"
        ).latest("id")
        self.assertEqual((entry.old_value, entry.new_value, entry.user), ("***", "***", user))

    def test_clear_secret(self):
        self.login()
        self.save(sandbox={"clear_client_secret": "on"})
        sandbox = self.connection.credentials.get(environment="sandbox")
        self.assertFalse(sandbox.has_secret("client_secret"))
        self.assertTrue(self.prod().has_secret("client_secret"))
        response = self.client.get(self.change_url())
        self.assertContains(response, "не задан")

    def test_value_and_clear_together_refused(self):
        self.login()
        response = self.save(prod={"password": "x", "clear_password": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "не одновременно")
        self.assertEqual(self.prod().secret("password"), CREDENTIALS["password"])
        self.assertNoSecrets(response)

    def test_form_with_error_does_not_echo_typed_secret(self):
        self.login()
        response = self.save(prod={"password": "typed-SECRET-123", "clear_password": "on"})
        self.assertNotContains(response, "typed-SECRET-123")

    def test_history_has_no_secrets(self):
        self.login()
        self.save(prod={"password": "second-password-VALUE"})
        response = self.client.get(f"{self.change_url()[:-len('change/')]}history/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "second-password-VALUE")
        self.assertNoSecrets(response)

    def test_change_forgets_token(self):
        self.login()
        self.fake.answers.append(token_answer())
        self.client.post(f"{LIST_URL}{self.connection.pk}/check/prod/")
        self.assertIsNotNone(ersp.token_status(self.mosaic, "prod"))
        self.save(prod={"password": "new-password"})
        self.assertIsNone(ersp.token_status(self.mosaic, "prod"))

    def test_unchanged_save_keeps_token(self):
        self.login()
        self.fake.answers.append(token_answer())
        self.client.post(f"{LIST_URL}{self.connection.pk}/check/prod/")
        self.save()
        self.assertIsNotNone(ersp.token_status(self.mosaic, "prod"))


class CheckTests(ScreenCase):
    def check(self, environment="prod"):
        return self.client.post(f"{LIST_URL}{self.connection.pk}/check/{environment}/", follow=True)

    def test_success_shown_with_token_state(self):
        user = self.login()
        self.fake.answers.append(token_answer(expires_in=1051898))
        response = self.check()
        self.assertContains(response, "Подключение к ЕРСП (боевая среда) работает")
        self.assertContains(response, "12 дн.")
        self.assertContains(response, "действует до")
        self.assertContains(response, "data-ersp-countdown")
        self.assertNotContains(response, "access-token-VALUE")
        exchange = Exchange.objects.get()
        self.assertEqual((exchange.user, exchange.insurance_company), (user, self.mosaic))
        self.assertEqual(self.fake.calls[0]["body"]["username"], "mosaic_ins")

    def test_sandbox_uses_its_own_address(self):
        self.login()
        self.fake.answers.append(token_answer())
        self.check("sandbox")
        self.assertTrue(self.fake.calls[0]["url"].startswith("https://sandboxerspapiv2."))

    def test_failure_shown(self):
        self.login()
        self.fake.answers.append((400, {"error": "invalid_grant", "error_message": "Invalid username and password"}))
        response = self.check()
        self.assertContains(response, "не работает")
        self.assertContains(response, "Invalid username and password")
        self.assertIsNone(ersp.token_status(self.mosaic, "prod"))

    def test_incomplete_card_explained_without_request(self):
        self.login()
        self.save(prod={"clear_password": "on"})
        response = self.check()
        self.assertContains(response, "нет: пароль")
        self.assertEqual(self.fake.calls, [])

    def test_no_token_yet(self):
        self.login()
        self.assertContains(self.client.get(self.change_url()), "будет получен при первом запросе")

    def test_only_post(self):
        self.login()
        with refusal_expected(self):
            self.assertEqual(self.client.get(f"{LIST_URL}{self.connection.pk}/check/prod/").status_code, 405)

    def test_unknown_environment(self):
        self.login()
        with refusal_expected(self):
            self.assertEqual(self.client.post(f"{LIST_URL}{self.connection.pk}/check/test/").status_code, 403)


class SendEnvironmentTests(ScreenCase):
    def url(self):
        return f"{LIST_URL}{self.connection.pk}/send-environment/"

    def test_page_explains(self):
        self.login()
        response = self.client.get(self.url())
        self.assertContains(response, "настоящий реестр регулятора")
        self.assertContains(self.client.get(self.change_url()), "Среда отправки полисов")

    def test_prod_needs_confirmation(self):
        self.login()
        response = self.client.post(self.url(), {"send_environment": "prod"})
        self.assertContains(response, "отметьте подтверждение")
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.send_environment, "sandbox")

    def test_switch_logged(self):
        user = self.login()
        response = self.client.post(self.url(), {"send_environment": "prod", "confirm": "yes"})
        self.assertRedirects(response, self.change_url(), fetch_redirect_response=False)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.send_environment, "prod")
        self.assertEqual(ersp.environment_for(ersp.SEND, self.mosaic), "prod")
        entry = ChangeLog.objects.get(field="send_environment", action="update")
        self.assertEqual((entry.old_value, entry.new_value, entry.user), ("sandbox", "prod", user))
        # Обратно в песочницу - без подтверждения.
        self.client.post(self.url(), {"send_environment": "sandbox"})
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.send_environment, "sandbox")

    def test_reading_stays_on_prod(self):
        self.login()
        self.client.post(self.url(), {"send_environment": "prod", "confirm": "yes"})
        self.client.post(self.url(), {"send_environment": "sandbox"})
        self.assertEqual(ersp.environment_for(ersp.PROVIDER, self.mosaic), "prod")
        self.assertEqual(ersp.environment_for(ersp.REFERENCES, self.mosaic), "prod")

