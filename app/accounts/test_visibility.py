"""
Автотесты общей функции видимости и правила «роль - видимость» (часть B4;
модель: docs/users-model.md, «Видимость данных»). Видимость агентов -
agents/test_visibility.py.

Главное, что проверяется:
- что видит человек по своему прикреплению (вид Scope);
- роль «Работа с агентами» с видимостью «свои» не сохраняется: ни через
  блок «Прикрепления», ни через «Принять» / «Перевести», ни в обход формы
  (Assignment.set_roles) - ошибкой с понятным текстом, а не 500.

Запуск на сервере: bin/test accounts
"""
from django.core.exceptions import ValidationError

from companies.models import Department

from .forms import TransferForm
from .models import Assignment, Role, User, Visibility
from . import test_assignments
from .test_assignments import USER_URL, AssignmentCase, D
from .visibility import Kind, scope_for

TOO_NARROW = "видимость «свои» не подходит"


class ScopeKindTests(AssignmentCase):
    def test_kinds(self):
        place = self.assign(self.bekzod, department=self.accounting, valid_from=D(2020, 1, 1))
        self.assertEqual(scope_for(self.bekzod).kind, Kind.COMPANY)
        place.visibility = Visibility.DEPARTMENT
        place.save(by=self.root)
        scope = scope_for(self.bekzod)
        self.assertEqual((scope.kind, scope.department_ids), (Kind.DEPARTMENTS, frozenset({self.accounting.pk})))
        place.visibility = Visibility.OWN
        place.save(by=self.root)
        self.assertEqual((scope_for(self.bekzod).kind, scope_for(self.bekzod).user_id), (Kind.OWN, self.bekzod.pk))

    def test_place_by_date(self):
        # Видимость - по прикреплению на дату: до начала работы - ничего.
        self.assign(self.bekzod, valid_from=D(2026, 10, 1))
        self.assertEqual(scope_for(self.bekzod, D(2026, 9, 30)).kind, Kind.NONE)
        self.assertEqual(scope_for(self.bekzod, D(2026, 10, 1)).kind, Kind.COMPANY)

    def test_blocked_and_superuser(self):
        self.assign(self.bekzod, valid_from=D(2020, 1, 1))
        self.bekzod.is_active = False
        self.assertEqual(scope_for(self.bekzod).kind, Kind.NONE)
        self.assertEqual(scope_for(self.root).kind, Kind.ALL)
        emergency = User.objects.create_superuser("emergency", insurance_company=self.mosaic)
        self.assertEqual((scope_for(emergency).kind, scope_for(emergency).company_id), (Kind.COMPANY, self.mosaic.pk))

    def test_departments_list(self):
        self.assign(self.bekzod, department=self.accounting, valid_from=D(2020, 1, 1),
                    visibility=Visibility.DEPARTMENT)
        everything = Department.objects.all()
        self.assertEqual(list(scope_for(self.bekzod).departments(everything)), [self.accounting])
        self.assertEqual(set(scope_for(self.root).departments(everything)), set(everything))


class AgentsRoleNeedsDepartmentTests(AssignmentCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.agents_role = Role.objects.get(code="agents")
        cls.employee_role = Role.objects.get(code="employee")

    def test_set_roles_refuses(self):
        place = self.assign(visibility=Visibility.OWN)
        with self.assertRaises(ValidationError) as caught:
            place.set_roles([self.agents_role], by=self.root)
        self.assertIn(TOO_NARROW, str(caught.exception))
        self.assertFalse(place.roles.exists())
        place.set_roles([self.employee_role], by=self.root)  # другие роли - можно

    def test_other_visibility_allowed(self):
        for visibility in (Visibility.DEPARTMENT, Visibility.COMPANY):
            place = self.assign(user=self.user(f"u_{visibility}"), visibility=visibility)
            place.set_roles([self.agents_role], by=self.root)

    def test_transfer_form(self):
        form = TransferForm(data={
            "valid_from": "2026-10-01", "department": self.accounting.pk, "position": self.analyst.pk,
            "roles": [self.agents_role.pk], "visibility": Visibility.OWN, "basis": "",
        })
        form.limit_places(self.mosaic.pk)
        self.assertFalse(form.is_valid())
        self.assertIn(TOO_NARROW, form.errors["visibility"][0])


class AgentsRoleOnScreenTests(AssignmentCase):
    # Помощник отправки формы карточки - из тестов блока прикреплений
    # (через модуль: импорт самого класса прогнал бы его тесты ещё раз).
    user_data = test_assignments.AssignmentAdminTests.user_data

    def setUp(self):
        self.client.force_login(self.root)

    def test_message_not_500(self):
        place = self.assign(roles=[Role.objects.get(code="agents")])
        data = self.user_data(self.bekzod, [{
            "id": place.pk, "valid_from": "2026-10-01", "department": self.head_office.pk,
            "position": self.analyst.pk, "roles": list(place.roles.all()),
        }])
        data["assignments-0-visibility"] = Visibility.OWN
        page = self.client.post(f"{USER_URL}{self.bekzod.pk}/change/", data)
        self.assertContains(page, TOO_NARROW)
        self.assertEqual(Assignment.objects.get(pk=place.pk).visibility, Visibility.COMPANY)
        # Ошибка - над прикреплением, а не над полем: «Видимость» стоит в паре
        # с «Руководитель подразделения», и текст над полем разводил пару (21.10).
        form = page.context["inline_admin_formsets"][0].formset.forms[0]
        self.assertNotIn("visibility", form.errors)
        self.assertIn(TOO_NARROW, " ".join(form.non_field_errors()))
