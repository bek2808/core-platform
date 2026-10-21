"""
Автотесты должностей сотрудников, ролей и прикреплений (часть A2.2).

Главное, что проверяется:
- на дату у человека одно прикрепление, у подразделения один руководитель -
  и в коде (сообщением), и в базе (ограничение исключения, btree_gist);
- подразделение и менеджер ассистента - только своей страховой (в коде
  и триггером в базе); владелец прикрепления не меняется;
- смена ролей пишется в журнал и видна только своей страховой;
- закрыть подразделение, где работают люди, нельзя;
- экран: блок прикреплений в карточке пользователя, пересечение периодов
  в одной отправке формы - сообщением, а не ошибкой 500.

Права людей в этой части не меняются: прикрепления правит только
суперпользователь (права на них администратору страховой - часть A3).

Запуск на сервере: bin/test accounts
"""
import datetime
import re

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from audit.models import ChangeLog, UntrackedChangeError
from companies.models import Department, InsuranceCompany

from .models import Assignment, JobPosition, Role, User, Visibility, current_assignment
from .testing import give_roles

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
D = datetime.date
USER_URL = "/admin/accounts/user/"


class AssignmentCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mosaic = InsuranceCompany.objects.create(name="Mosaic", code="mosaic", country="KZ")
        cls.other = InsuranceCompany.objects.create(name="Other", code="other", country="KZ")
        cls.head_office = Department.head_office_for(cls.mosaic)
        cls.other_office = Department.head_office_for(cls.other)
        cls.accounting = Department(insurance_company=cls.mosaic, kind="division", name="Бухгалтерия", parent=cls.head_office)
        cls.accounting.save()
        cls.analyst = JobPosition.objects.get(name="Бизнес-аналитик")
        cls.accountant = JobPosition.objects.get(name="Главный бухгалтер")
        cls.root = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        cls.bekzod = cls.user("bekzod")
        cls.aziza = cls.user("aziza")
        cls.stranger = cls.user("stranger", company=cls.other)

    @classmethod
    def user(cls, username, company=None):
        user = User(username=username, insurance_company=company or cls.mosaic, last_name=username.title())
        user.set_password(STRONG_PASSWORD)
        user.save()
        return user

    def assign(self, user=None, check=True, roles=(), **fields):
        """Прикрепление так же, как экран: проверка, сохранение, роли."""
        fields.setdefault("department", self.head_office)
        fields.setdefault("position", self.analyst)
        fields.setdefault("valid_from", D(2026, 10, 1))
        assignment = Assignment(user=user or self.bekzod, **fields)
        if check:
            assignment.full_clean()
        assignment.save(by=self.root)
        if roles:
            assignment.set_roles(roles, by=self.root)
        return assignment

    def errors(self, assignment):
        with self.assertRaises(ValidationError) as caught:
            assignment.full_clean()
        return caught.exception.message_dict


class StarterListsTests(AssignmentCase):
    def test_positions_from_mosaic_site_and_not_specified(self):
        names = set(JobPosition.objects.values_list("name", flat=True))
        for name in ["Генеральный директор", "Бизнес-аналитик", "Андеррайтер", "Ассистент", "Не указана"]:
            self.assertIn(name, names)

    def test_roles_from_existing_groups(self):
        roles = {role.code: role.group.name for role in Role.objects.select_related("group")}
        self.assertEqual(roles, {
            "company_admin": "Администратор страховой",
            "references_admin": "Администратор справочников",
            "integrations_admin": "Администратор интеграций",
            "employee": "Сотрудник",
        })
        self.assertTrue(all(role.audience == "company" for role in Role.objects.all()))


class JobPositionTests(AssignmentCase):
    def test_name_squashed_and_capitalized(self):
        position = JobPosition(name="  старший   андеррайтер ")
        position.full_clean()
        position.save()
        self.assertEqual(position.name, "Старший андеррайтер")

    def test_name_unique_ignoring_case(self):
        with self.assertRaises(ValidationError):
            JobPosition(name="бизнес-аналитик").full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            JobPosition(name="БИЗНЕС-АНАЛИТИК ").save()

    def test_no_delete(self):
        with self.assertRaises(UntrackedChangeError):
            self.analyst.delete()


class AssignmentRulesTests(AssignmentCase):
    def test_current_assignment(self):
        old = self.assign(valid_to=D(2026, 10, 10))
        new = self.assign(department=self.accounting, valid_from=D(2026, 10, 11))
        self.assertEqual(current_assignment(self.bekzod, D(2026, 10, 5)), old)
        self.assertEqual(current_assignment(self.bekzod, D(2026, 10, 11)), new)
        self.assertIsNone(current_assignment(self.bekzod, D(2026, 9, 30)))
        self.assertTrue(new.is_current(D(2030, 1, 1)))

    def test_dates_order(self):
        wrong = Assignment(user=self.bekzod, department=self.head_office, position=self.analyst,
                           valid_from=D(2026, 10, 10), valid_to=D(2026, 10, 9))
        self.assertIn("«Действует по» не может быть раньше", str(self.errors(wrong)))
        with self.assertRaises(IntegrityError), transaction.atomic():
            wrong.save()

    def test_one_day_assignment_allowed(self):
        self.assign(valid_from=D(2026, 10, 10), valid_to=D(2026, 10, 10))

    def test_overlap_refused_with_message_and_by_database(self):
        self.assign(valid_from=D(2026, 10, 1))  # без даты «по» - по сей день
        later = Assignment(user=self.bekzod, department=self.accounting, position=self.analyst, valid_from=D(2027, 1, 1))
        self.assertIn("уже есть другое прикрепление", str(self.errors(later)))
        with self.assertRaises(IntegrityError), transaction.atomic():
            later.save()

    def test_overlap_on_boundary_day(self):
        # «По» включительно: 10.10 у обоих - пересечение.
        self.assign(valid_to=D(2026, 10, 10))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.assign(department=self.accounting, valid_from=D(2026, 10, 10), check=False)

    def test_next_day_allowed(self):
        self.assign(valid_to=D(2026, 10, 10))
        self.assign(department=self.accounting, valid_from=D(2026, 10, 11))

    def test_other_people_do_not_overlap(self):
        self.assign()
        self.assign(user=self.aziza)

    def test_one_head_per_department(self):
        self.assign(department=self.accounting, is_head=True)
        second = Assignment(user=self.aziza, department=self.accounting, position=self.accountant,
                            valid_from=D(2026, 10, 5), is_head=True)
        self.assertIn("уже есть руководитель", str(self.errors(second)))
        with self.assertRaises(IntegrityError), transaction.atomic():
            second.save()

    def test_new_head_after_old_one_left(self):
        self.assign(department=self.accounting, is_head=True, valid_to=D(2026, 10, 31))
        self.assign(user=self.aziza, department=self.accounting, position=self.accountant,
                    valid_from=D(2026, 11, 1), is_head=True)

    def test_head_sees_at_least_department(self):
        wrong = Assignment(user=self.bekzod, department=self.accounting, position=self.analyst,
                           valid_from=D(2026, 10, 1), is_head=True, visibility=Visibility.OWN)
        self.assertIn("visibility", self.errors(wrong))
        with self.assertRaises(IntegrityError), transaction.atomic():
            wrong.save()

    def test_department_of_other_company(self):
        wrong = Assignment(user=self.bekzod, department=self.other_office, position=self.analyst, valid_from=D(2026, 10, 1))
        self.assertIn("другой страховой", self.errors(wrong)["department"][0])
        with self.assertRaises(IntegrityError), transaction.atomic():
            wrong.save()  # триггер в базе

    def test_user_without_company_has_no_assignments(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment(user=self.root, department=self.head_office, position=self.analyst, valid_from=D(2026, 10, 1)).save()

    def test_closed_department_for_new_assignment(self):
        self.accounting.status = Department.Status.CLOSED
        self.accounting.save()
        wrong = Assignment(user=self.bekzod, department=self.accounting, position=self.analyst, valid_from=D(2026, 10, 1))
        self.assertIn("закрыто", self.errors(wrong)["department"][0])

    def test_inactive_position_for_new_assignment(self):
        self.accountant.is_active = False
        self.accountant.save()
        wrong = Assignment(user=self.bekzod, department=self.head_office, position=self.accountant, valid_from=D(2026, 10, 1))
        self.assertIn("снята", self.errors(wrong)["position"][0])

    def test_old_assignment_keeps_removed_position(self):
        assignment = self.assign(position=self.accountant)
        self.accountant.is_active = False
        self.accountant.save()
        assignment.valid_to = D(2026, 12, 31)
        assignment.full_clean()  # должность сняли позже - прежнее прикрепление правится
        assignment.save(by=self.root)

    def test_assistant_rules(self):
        no_scope = Assignment(user=self.aziza, department=self.head_office, position=self.analyst,
                              valid_from=D(2026, 10, 1), assistant_to=self.bekzod)
        self.assertIn("assistant_scope", self.errors(no_scope))
        own = Assignment(user=self.bekzod, department=self.head_office, position=self.analyst,
                         valid_from=D(2026, 10, 1), assistant_to=self.bekzod, assistant_scope="all")
        self.assertIn("assistant_to", self.errors(own))
        with self.assertRaises(IntegrityError), transaction.atomic():
            own.save()
        self.assign(user=self.aziza, assistant_to=self.bekzod, assistant_scope="entered")

    def test_scope_cleared_without_manager(self):
        assignment = self.assign(assistant_scope="all")  # менеджера нет - «видит» не нужно
        self.assertEqual(assignment.assistant_scope, "")

    def test_assistant_of_other_company(self):
        wrong = Assignment(user=self.aziza, department=self.head_office, position=self.analyst,
                           valid_from=D(2026, 10, 1), assistant_to=self.stranger, assistant_scope="all")
        self.assertIn("другой страховой", self.errors(wrong)["assistant_to"][0])
        with self.assertRaises(IntegrityError), transaction.atomic():
            wrong.save()

    def test_owner_cannot_change(self):
        assignment = self.assign()
        assignment.user = self.aziza
        self.assertIn("user", self.errors(assignment))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment._base_manager.filter(pk=assignment.pk).update(user=self.aziza)

    def test_no_delete(self):
        with self.assertRaises(UntrackedChangeError):
            self.assign().delete()


class RolesJournalTests(AssignmentCase):
    def roles_rows(self, assignment):
        return ChangeLog.objects.filter(object_id=assignment.pk, content_type__model="assignment", field="roles")

    def test_roles_change_logged_with_names(self):
        admin_role = Role.objects.get(code="company_admin")
        employee = Role.objects.get(code="employee")
        assignment = self.assign(roles=[employee])
        assignment.set_roles([employee, admin_role], by=self.bekzod)
        first, second = self.roles_rows(assignment).order_by("id")
        self.assertEqual((first.old_value, first.new_value), (None, "Сотрудник"))
        self.assertEqual(
            (second.user, second.old_value, second.new_value),
            (self.bekzod, "Сотрудник", "Администратор страховой, Сотрудник"),
        )

    def test_same_roles_not_logged_again(self):
        employee = Role.objects.get(code="employee")
        assignment = self.assign(roles=[employee])
        assignment.set_roles([employee], by=self.root)
        self.assertEqual(self.roles_rows(assignment).count(), 1)

    def test_journal_rows_belong_to_user_company_and_card(self):
        assignment = self.assign(roles=[Role.objects.get(code="employee")])
        rows = ChangeLog.objects.filter(object_id=assignment.pk, content_type__model="assignment")
        self.assertTrue(rows.exists())
        for row in rows:
            # Иначе строку увидели бы все страховые (журнал показывает своё и общее).
            self.assertEqual(row.insurance_company, self.mosaic)
            self.assertEqual((row.root_content_type.model, row.root_object_id), ("user", self.bekzod.pk))


class DepartmentClosingTests(AssignmentCase):
    def test_cannot_close_where_people_work(self):
        self.assign(department=self.accounting, valid_from=D(2020, 1, 1))
        self.accounting.status = Department.Status.CLOSED
        with self.assertRaises(ValidationError) as caught:
            self.accounting.full_clean()
        self.assertIn("Здесь работают люди: Bekzod", caught.exception.message_dict["status"][0])

    def test_can_close_after_assignments_ended(self):
        self.assign(department=self.accounting, valid_from=D(2020, 1, 1), valid_to=D(2020, 12, 31))
        self.accounting.status = Department.Status.CLOSED
        self.accounting.full_clean()


class AssignmentAdminTests(AssignmentCase):
    """Блок «Прикрепления» в карточке пользователя."""

    def user_data(self, user, rows):
        data = {
            "username": user.username, "last_name": user.last_name, "first_name": "", "patronymic": "",
            "email": "", "phone": "", "pinfl": "", "kind": "employee",
            "is_active": "on", "is_staff": "on",
            "assignments-TOTAL_FORMS": str(len(rows)),
            "assignments-INITIAL_FORMS": str(sum(1 for row in rows if "id" in row)),
            "assignments-MIN_NUM_FORMS": "0", "assignments-MAX_NUM_FORMS": "1000",
        }
        for index, row in enumerate(rows):
            prefix = f"assignments-{index}-"
            data[prefix + "user"] = str(user.pk)
            data[prefix + "visibility"] = "company"
            data[prefix + "assistant_scope"] = ""
            for field, value in row.items():
                if field == "roles":
                    data[prefix + "roles"] = [str(role.pk) for role in value]
                elif value is True:
                    data[prefix + field] = "on"
                elif value is not False:
                    data[prefix + field] = str(value)
        return data

    def post(self, user, rows):
        return self.client.post(f"{USER_URL}{user.pk}/change/", self.user_data(user, rows))

    def setUp(self):
        self.client.force_login(self.root)

    def test_block_on_change_page_only(self):
        self.assertContains(self.client.get(f"{USER_URL}{self.bekzod.pk}/change/"), "assignments-TOTAL_FORMS")
        self.assertNotContains(self.client.get(f"{USER_URL}add/"), "assignments-TOTAL_FORMS")

    def options(self, page, field):
        """Значения выпадающего списка поля в образце новой строки блока."""
        select = re.search(rf'<select name="assignments-__prefix__-{field}".*?</select>', page, re.S).group(0)
        return set(re.findall(r'<option value="(\d+)"', select))

    def test_lists_offer_only_users_company(self):
        page = self.client.get(f"{USER_URL}{self.bekzod.pk}/change/").content.decode()
        self.assertIn("Головной офис › Бухгалтерия", page)
        self.assertEqual(self.options(page, "department"), {str(self.head_office.pk), str(self.accounting.pk)})
        # Менеджер ассистента - коллеги из той же страховой, без самого человека.
        self.assertEqual(self.options(page, "assistant_to"), {str(self.aziza.pk)})

    def test_add_assignment_with_roles_through_screen(self):
        employee = Role.objects.get(code="employee")
        admin_role = Role.objects.get(code="company_admin")
        page = self.post(self.bekzod, [{
            "valid_from": "2026-10-01", "department": self.accounting.pk, "position": self.analyst.pk,
            "roles": [employee, admin_role], "basis": "  Приказ  № 5 ",
        }])
        self.assertEqual(page.status_code, 302, page.content.decode()[-3000:])
        assignment = Assignment.objects.get(user=self.bekzod)
        self.assertEqual(set(assignment.roles.values_list("code", flat=True)), {"employee", "company_admin"})
        self.assertEqual((assignment.basis, assignment.created_by), ("Приказ № 5", self.root))
        row = ChangeLog.objects.get(object_id=assignment.pk, content_type__model="assignment", field="roles")
        self.assertEqual((row.user, row.new_value), (self.root, "Администратор страховой, Сотрудник"))

    def test_overlapping_rows_in_one_form_show_message(self):
        page = self.post(self.bekzod, [
            {"valid_from": "2026-10-01", "department": self.head_office.pk, "position": self.analyst.pk},
            {"valid_from": "2026-10-05", "department": self.accounting.pk, "position": self.analyst.pk},
        ])
        self.assertContains(page, "Периоды прикреплений пересекаются")  # не ошибка 500
        self.assertFalse(Assignment.objects.exists())

    def test_overlap_with_saved_row_shows_message(self):
        saved = self.assign(valid_from=D(2026, 10, 1))
        page = self.post(self.bekzod, [
            {"id": saved.pk, "valid_from": "2026-10-01", "department": self.head_office.pk, "position": self.analyst.pk},
            {"valid_from": "2027-01-01", "department": self.accounting.pk, "position": self.analyst.pk},
        ])
        self.assertContains(page, "Периоды прикреплений пересекаются")

    def test_transfer_by_closing_old_and_opening_new(self):
        saved = self.assign(valid_from=D(2026, 10, 1))
        page = self.post(self.bekzod, [
            {"id": saved.pk, "valid_from": "2026-10-01", "valid_to": "2026-10-20",
             "department": self.head_office.pk, "position": self.analyst.pk},
            {"valid_from": "2026-10-21", "department": self.accounting.pk, "position": self.analyst.pk},
        ])
        self.assertEqual(page.status_code, 302, re.findall(r'class="errorlist[^>]*>.*?</ul>', page.content.decode(), re.S))
        self.assertEqual(current_assignment(self.bekzod, D(2026, 10, 21)).department, self.accounting)

    def test_reversed_dates_show_message(self):
        # С такими датами PostgreSQL не строит период: без проверки в clean()
        # проверка пересечений упала бы с ошибкой базы (500).
        page = self.post(self.bekzod, [
            {"valid_from": "2026-10-10", "valid_to": "2026-10-01",
             "department": self.head_office.pk, "position": self.analyst.pk},
        ])
        self.assertContains(page, "не может быть раньше")
        self.assertFalse(Assignment.objects.exists())

    def test_other_company_department_refused(self):
        page = self.post(self.bekzod, [
            {"valid_from": "2026-10-01", "department": self.other_office.pk, "position": self.analyst.pk},
        ])
        self.assertEqual(page.status_code, 200)  # форма с ошибкой, не 500
        self.assertFalse(Assignment.objects.exists())

    def test_second_head_shows_message(self):
        self.assign(user=self.aziza, department=self.accounting, is_head=True)
        page = self.post(self.bekzod, [
            {"valid_from": "2026-10-01", "department": self.accounting.pk, "position": self.analyst.pk, "is_head": True},
        ])
        self.assertContains(page, "уже есть руководитель")

    def test_block_layout(self):
        # Ровные строки: без пояснений под полями пары, без значков ✎ + 👁,
        # пустой пункт списка - по-русски (в Django 6.1 он «- Select an option -»).
        page = self.client.get(f"{USER_URL}{self.bekzod.pk}/change/").content.decode()
        self.assertNotIn("Select an option", page)
        self.assertNotIn("Включительно. Пусто", page)  # пояснение поля «по» - в подсказке блока
        self.assertIn("«Действует по» - включительно", page)
        for field in ("department", "position", "assistant_to"):
            self.assertNotIn(f'id="add_id_assignments-__prefix__-{field}"', page)
            self.assertNotIn(f'id="change_id_assignments-__prefix__-{field}"', page)

    def test_list_shows_current_place(self):
        self.assign(department=self.accounting, valid_from=D(2020, 1, 1))
        self.assertContains(self.client.get(USER_URL), "Головной офис › Бухгалтерия, Бизнес-аналитик")

    def test_user_history_shows_assignment_changes(self):
        assignment = self.assign(roles=[Role.objects.get(code="employee")])
        page = self.client.get(f"{USER_URL}{self.bekzod.pk}/history/", follow=True)
        self.assertContains(page, "Сотрудник")
        self.assertContains(page, str(assignment)[:20])

    def test_company_admin_without_rights_sees_no_block(self):
        # Без прав на прикрепления (только на пользователей) блока не видно.
        company_admin = User.objects.create_user(
            "company_admin", password=STRONG_PASSWORD, is_staff=True, insurance_company=self.mosaic,
        )
        company_admin.user_permissions.set(
            Permission.objects.filter(content_type__app_label="accounts", codename__in=["view_user", "change_user"])
        )
        give_roles(company_admin)  # место без ролей: без места не войти (A3)
        self.client.force_login(company_admin)
        page = self.client.get(f"{USER_URL}{self.bekzod.pk}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "assignments-TOTAL_FORMS")


class CatalogAdminTests(AssignmentCase):
    def setUp(self):
        self.client.force_login(self.root)

    def test_roles_read_only_even_for_superuser(self):
        self.assertContains(self.client.get("/admin/accounts/role/"), "Администратор страховой")
        from config.testing import refusal_expected
        with refusal_expected(self):
            page = self.client.get("/admin/accounts/role/add/")
        self.assertEqual(page.status_code, 403)

    def test_add_position_through_screen(self):
        page = self.client.post("/admin/accounts/jobposition/add/", {"name": "актуарий", "sort_order": "50", "is_active": "on"})
        self.assertEqual(page.status_code, 302)
        position = JobPosition.objects.get(name="Актуарий")
        self.assertEqual(position.created_by, self.root)

    def test_duplicate_position_shows_message(self):
        page = self.client.post("/admin/accounts/jobposition/add/", {"name": "андеррайтер", "sort_order": "50", "is_active": "on"})
        self.assertContains(page, "Такая должность уже есть")
