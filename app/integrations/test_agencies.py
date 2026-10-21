"""
Автотесты справочника ЕРСП «Агенты/филиалы» (integrations.ErspAgency,
модель: docs/users-model.md, «Связь с ЕРСП: agencyId»).

Главное, что проверяется:
- клиент ЕРСП читает справочник запросом GET без тела, ответ - список,
  запрос попадает в журнал обмена;
- загрузка идёт из среды отправки полисов страховой (сейчас песочница);
- справочник хранится отдельно по страховой и среде, как прислал ЕРСП;
- повторная загрузка ничего не дублирует, пропавшая запись помечается,
  удалённая в ЕРСП - получает отметку «удалён»;
- ошибка в ответе - в базе не меняется ничего;
- экран: только просмотр своей страховой; администратору платформы - 403.

В сеть тесты не ходят (FakeErsp). Запуск на сервере: bin/test integrations
"""
import copy
import io
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from accounts.models import User
from accounts.testing import give_roles
from companies.models import InsuranceCompany
from config.testing import refusal_expected
from references.loader import LoadError

from .agencies import load_agencies, sync_agencies
from .ersp import SEND, ErspClient, ErspError
from .models import Environment, ErspAgency, ErspConnection, Exchange
from .tests import ErspMixin, FakeErsp, token_answer

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
LIST_URL = "/admin/integrations/erspagency/"

# Строки из настоящего ответа боевой среды для Mosaic (требования Бекзода 21.10).
HEAD_OFFICE = {
    "id": 1600, "name": "Головной Офис", "inn": "308143734", "agencyType": 1, "areaTypeId": None,
    "formationDate": None, "agentAgreementNumber": "", "agentAgreementDate": None,
    "isElectronAgency": False, "isHeadElectronAgency": None, "insuranceTypesAndComissions": [],
    "person": None, "organization": {"type": 2, "inn": "308143734", "name": "АО СО \"MOSAIC INSURANCE GROUP\""},
}
RESERVE = {**HEAD_OFFICE, "id": 1599, "name": "Головной Офис (резерв)", "parentId": 1600, "areaTypeId": 1}
BROKER = {
    "id": 834, "name": "Broker: ООО \"InRe\"", "inn": "201070533", "parentId": 1600, "agencyType": 2,
    "areaTypeId": 1, "formationDate": None, "agentAgreementNumber": "AZM/21-000",
    "agentAgreementDate": "2021-04-14", "isElectronAgency": False, "isHeadElectronAgency": None,
    "insuranceTypesAndComissions": [], "person": None,
    "organization": {"type": 2, "inn": "201070533", "name": "ООО \"InRe\""},
}
# Агент-ИП, по ошибке ввода заведённый в ЕРСП «филиалом» с ИНН Mosaic: храним как есть.
TOIROVA = {
    **HEAD_OFFICE, "id": 1598, "name": "Агент: TOIROVA SHAXLO MURODJON QIZI ИП", "parentId": 1600,
    "areaTypeId": 1, "formationDate": "2021-01-01", "agentAgreementNumber": "03",
}
PROD_ROWS = [BROKER, HEAD_OFFICE, RESERVE, TOIROVA]
# В песочнице у Mosaic (снимок экрана ЕРСП 21.10): 53 «direct dep» действует, 1084 «test» удалён.
SANDBOX_ROWS = [
    {"id": 53, "name": "direct dep", "inn": "308143734", "agencyType": 1, "person": None},
]


def agencies_answer(rows):
    return 200, {"error": 0, "error_message": "", "result": copy.deepcopy(rows)}


class ClientTests(ErspMixin, TestCase):
    def client_for(self, fake, environment=Environment.SANDBOX):
        return ErspClient(environment, self.mosaic, transport=fake)

    def test_get_without_body_and_journal(self):
        fake = FakeErsp(token_answer(), agencies_answer(SANDBOX_ROWS))
        rows = self.client_for(fake).references_agencies()
        self.assertEqual(rows, SANDBOX_ROWS)
        call = fake.calls[1]
        self.assertEqual(call["method"], "GET")
        self.assertIsNone(call["body"])
        self.assertNotIn("Content-Type", call["headers"])  # тела нет - и заголовка нет
        self.assertEqual(fake.paths()[1], "/api/references/agencies")
        log = Exchange.objects.get(operation=Exchange.Operation.REFERENCES_AGENCIES)
        self.assertEqual((log.method, log.outcome, log.environment), ("GET", "ok", "sandbox"))
        self.assertIsNone(log.request_body)
        self.assertEqual(log.response_body["result"][0]["id"], 53)

    def test_provider_still_posts_json(self):
        # Запросы с телом - как раньше: POST и JSON.
        fake = FakeErsp(token_answer(), (200, {"error": 0, "result": {"name": "X"}}))
        self.client_for(fake, Environment.PROD).provider_inn("207078596")
        self.assertEqual(fake.calls[1]["method"], "POST")
        self.assertEqual(fake.calls[1]["headers"]["Content-Type"], "application/json")

    def test_answer_without_list_is_error(self):
        fake = FakeErsp(token_answer(), (200, {"error": 0, "result": {"id": 53}}))
        with self.assertRaisesMessage(ErspError, "нет данных"):
            self.client_for(fake).references_agencies()
        self.assertEqual(Exchange.objects.get(operation="references_agencies").outcome, "bad_response")

    def test_ersp_error_reported(self):
        fake = FakeErsp(token_answer(), (200, {"error": 1, "error_message": "Доступ запрещён", "result": None}))
        with self.assertRaisesMessage(ErspError, "Доступ запрещён"):
            self.client_for(fake).references_agencies()

    def test_send_environment_from_connection_card(self):
        # Справочник агентов - из той среды, куда уходят полисы.
        self.assertEqual(ErspClient.for_purpose(SEND, self.mosaic).environment, "sandbox")
        connection = ErspConnection.objects.get(insurance_company=self.mosaic)
        connection.send_environment = Environment.PROD
        connection.save()
        self.assertEqual(ErspClient.for_purpose(SEND, self.mosaic).environment, "prod")


class SyncTests(ErspMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")

    def sync(self, rows=PROD_ROWS, environment=Environment.PROD, company=None):
        return sync_agencies(company or self.mosaic, environment, copy.deepcopy(rows))

    def test_first_load(self):
        result = self.sync()
        self.assertEqual((result.created, result.updated, result.hidden), (4, 0, 0))
        head = ErspAgency.objects.get(insurance_company=self.mosaic, environment="prod", ersp_id=1600)
        self.assertEqual(head.name, "Головной Офис")
        self.assertEqual(head.agency_type, ErspAgency.AgencyType.BRANCH)
        self.assertIsNone(head.parent_ersp_id)
        broker = ErspAgency.objects.get(ersp_id=834)
        self.assertEqual((broker.agency_type, broker.parent_ersp_id, broker.inn), (2, 1600, "201070533"))
        self.assertEqual(broker.raw, BROKER)
        self.assertTrue(broker.can_be_linked())

    def test_input_errors_of_ersp_kept(self):
        # Агент, заведённый в ЕРСП «филиалом» с ИНН Mosaic, - так и хранится.
        self.sync()
        toirova = ErspAgency.objects.get(ersp_id=1598)
        self.assertEqual((toirova.agency_type, toirova.inn), (ErspAgency.AgencyType.BRANCH, "308143734"))

    def test_second_load_changes_nothing(self):
        self.sync()
        result = self.sync()
        self.assertEqual((result.created, result.updated, result.unchanged, result.hidden), (0, 0, 4, 0))
        self.assertEqual(ErspAgency.objects.count(), 4)

    def test_changed_row_updated(self):
        self.sync()
        rows = copy.deepcopy(PROD_ROWS)
        rows[0]["name"] = "Broker: ООО \"InRe\" (новое)"
        result = self.sync(rows)
        self.assertEqual(result.updated, 1)
        self.assertEqual(ErspAgency.objects.get(ersp_id=834).name, "Broker: ООО \"InRe\" (новое)")

    def test_missing_row_marked_not_deleted(self):
        self.sync()
        result = self.sync([HEAD_OFFICE, RESERVE, TOIROVA])
        self.assertEqual(result.hidden, 1)
        broker = ErspAgency.objects.get(ersp_id=834)
        self.assertFalse(broker.is_present)
        self.assertFalse(broker.can_be_linked())
        self.sync()  # вернулась в ответ - снова есть
        self.assertTrue(ErspAgency.objects.get(ersp_id=834).is_present)

    def test_deletion_mark_understood(self):
        deleted = {"id": 1084, "name": "test", "inn": "000000001", "agencyType": 1, "deletionMark": "Удален"}
        active = {**SANDBOX_ROWS[0], "deletionMark": "Активен"}
        self.sync([deleted, active], environment=Environment.SANDBOX)
        self.assertTrue(ErspAgency.objects.get(ersp_id=1084).is_deleted)
        self.assertFalse(ErspAgency.objects.get(ersp_id=1084).can_be_linked())
        self.assertFalse(ErspAgency.objects.get(ersp_id=53).is_deleted)

    def test_environments_and_companies_kept_apart(self):
        # Один и тот же id - разные записи в разных средах и у разных страховых.
        self.sync()
        self.sync([{**SANDBOX_ROWS[0], "id": 1600}], environment=Environment.SANDBOX)
        self.sync([{**SANDBOX_ROWS[0], "id": 1600}], environment=Environment.PROD, company=self.other)
        self.assertEqual(ErspAgency.objects.filter(ersp_id=1600).count(), 3)
        # Загрузка песочницы не трогает боевую: там 1600 по-прежнему головной офис.
        self.assertEqual(ErspAgency.objects.get(insurance_company=self.mosaic, environment="prod", ersp_id=1600).name,
                         "Головной Офис")
        self.assertEqual(ErspAgency.objects.filter(is_present=False).count(), 0)

    def test_bad_rows_change_nothing(self):
        self.sync()
        for bad in ([{"name": "без id"}], [{"id": 5}], [BROKER, BROKER]):
            with self.subTest(bad=bad), self.assertRaises(LoadError):
                self.sync([HEAD_OFFICE, *bad])
        self.assertEqual(ErspAgency.objects.filter(is_present=True).count(), 4)
        self.assertEqual(ErspAgency.objects.count(), 4)

    def test_pinfl_of_person(self):
        person = {"id": 900, "name": "Агент: физлицо", "agencyType": 2,
                  "person": {"passportData": {"pinfl": "30101990000000"}}}
        self.sync([person])
        self.assertEqual(ErspAgency.objects.get(ersp_id=900).pinfl, "30101990000000")

    def test_load_agencies_uses_send_environment(self):
        fake = FakeErsp(token_answer(), agencies_answer(SANDBOX_ROWS))
        client = ErspClient.for_purpose(SEND, self.mosaic, transport=fake)
        environment, result = load_agencies(self.mosaic, client=client)
        self.assertEqual((environment, result.created), ("sandbox", 1))
        self.assertTrue(ErspAgency.objects.filter(environment="sandbox", ersp_id=53).exists())


class CommandTests(ErspMixin, TestCase):
    def run_command(self, *answers, company="mosaic"):
        fake = FakeErsp(token_answer(), *answers)
        out = io.StringIO()
        with mock.patch("integrations.ersp.urllib_transport", fake):
            call_command("load_ersp_agencies", "--company", company, stdout=out)
        return out.getvalue()

    def test_loads_and_prints_rows(self):
        out = self.run_command(agencies_answer(PROD_ROWS + SANDBOX_ROWS))
        self.assertIn("mosaic, среда «песочница»: новых 5", out)
        self.assertIn("834 -> 1600: агент, Broker: ООО \"InRe\", ИНН 201070533", out)
        self.assertIn("53: филиал, direct dep", out)

    def test_ersp_error_changes_nothing(self):
        with self.assertRaisesMessage(CommandError, "в базе ничего не изменилось"):
            self.run_command((500, b"Internal Server Error"))
        self.assertFalse(ErspAgency.objects.exists())
        self.assertTrue(Exchange.objects.filter(operation="references_agencies", outcome="http_error").exists())

    def test_unknown_company(self):
        with self.assertRaisesMessage(CommandError, "Нет страховой"):
            call_command("load_ersp_agencies", "--company", "nope", stdout=io.StringIO())


class AdminTests(ErspMixin, TestCase):
    """Экран справочника: только просмотр своей страховой."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")
        sync_agencies(cls.mosaic, Environment.SANDBOX, copy.deepcopy(SANDBOX_ROWS))
        sync_agencies(cls.other, Environment.SANDBOX, [{"id": 777, "name": "Чужой филиал", "agencyType": 1}])

    def login(self, *groups, company="mosaic", username="admin_mosaic"):
        user = User.objects.create_user(
            username, password=STRONG_PASSWORD,
            insurance_company=self.mosaic if company == "mosaic" else None,
        )
        give_roles(user, *groups)
        self.client.force_login(user)
        return user

    def test_company_admin_sees_only_own(self):
        self.login("Администратор страховой")
        page = self.client.get(LIST_URL)
        self.assertContains(page, "direct dep")
        self.assertNotContains(page, "Чужой филиал")
        stranger = ErspAgency.objects.get(ersp_id=777)
        self.assertEqual(self.client.get(f"{LIST_URL}{stranger.pk}/change/").status_code, 302)

    def test_integrations_admin_can_view(self):
        self.login("Администратор интеграций", username="integrator")
        self.assertContains(self.client.get(LIST_URL), "direct dep")

    def test_read_only_even_for_superuser(self):
        root = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        self.client.force_login(root)
        agency = ErspAgency.objects.get(ersp_id=53)
        with refusal_expected(self):
            self.assertEqual(self.client.get(f"{LIST_URL}add/").status_code, 403)
        with refusal_expected(self):
            self.client.post(f"{LIST_URL}{agency.pk}/change/", {"name": "Взлом"})
        self.assertEqual(ErspAgency.objects.get(ersp_id=53).name, "direct dep")

    def test_platform_admin_refused(self):
        self.login("Администратор платформы", company=None, username="platform")
        with refusal_expected(self):
            self.assertEqual(self.client.get(LIST_URL).status_code, 403)

    def test_employee_without_role_refused(self):
        self.login("Сотрудник", username="clerk")
        with refusal_expected(self):
            self.assertEqual(self.client.get(LIST_URL).status_code, 403)
