"""
Автотесты подразделений страховой (модель: docs/users-model.md).

Главное, что проверяется:
- головной офис создаётся вместе со страховой, он один и не закрывается;
- не больше двух уровней: отдел входит только в головной офис или филиал
  своей страховой (и в коде, и в базе);
- код - 2-6 цифр, не повторяется внутри страховой; регион - только у филиала;
- страховая, вид и «входит в» не меняются после создания (триггер в базе);
- экран: чужая страховая не видна, ошибки - сообщением, а не ошибкой 500;
  права - от лица обычного сотрудника группы, а не суперпользователя.

Запуск на сервере: bin/test companies
"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import User
from accounts.testing import give_roles
from audit.models import ChangeLog, UntrackedChangeError
from config.testing import refusal_expected
from references.loader import load_from_files

from .models import Department, InsuranceCompany

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
TASHKENT, JIZZAKH, FERGANA = 10, 13, 15
LIST_URL = "/admin/companies/department/"
ADD_URL = f"{LIST_URL}add/"


class DepartmentCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_from_files()  # справочники ЕРСП - один раз на группу тестов
        cls.mosaic = InsuranceCompany.objects.create(
            name="Mosaic Insurance Group", code="mosaic", ersp_insurer_id=1084, ersp_region_id=TASHKENT
        )
        cls.other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")
        cls.head = Department.objects.get(insurance_company=cls.mosaic, kind=Department.Kind.HEAD_OFFICE)
        cls.other_head = Department.objects.get(insurance_company=cls.other, kind=Department.Kind.HEAD_OFFICE)

    def make(self, kind=Department.Kind.DIVISION, company=None, check=True, **fields):
        """Завести подразделение так же, как экран: сначала проверка, потом сохранение."""
        if kind == Department.Kind.DIVISION:
            fields.setdefault("parent", self.head)
        if kind == Department.Kind.BRANCH:
            fields.setdefault("region_id", JIZZAKH)
        fields.setdefault("name", "Бухгалтерия")
        department = Department(insurance_company=company or self.mosaic, kind=kind, **fields)
        if check:
            department.full_clean()
        department.save()
        return department

    def errors(self, department):
        with self.assertRaises(ValidationError) as caught:
            department.full_clean()
        return caught.exception.message_dict


class HeadOfficeTests(DepartmentCase):
    def test_created_with_company(self):
        self.assertEqual(self.head.name, "Головной офис")
        self.assertIsNone(self.head.parent)
        self.assertEqual(self.head.code, "")
        self.assertEqual(self.head.status, Department.Status.ACTIVE)
        # Создала система: «кто» пусто, но запись в журнале есть.
        self.assertIsNone(self.head.created_by)
        self.assertTrue(ChangeLog.objects.filter(object_id=self.head.pk, field="name").exists())

    def test_only_one_per_company(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make(kind=Department.Kind.HEAD_OFFICE, name="Второй офис", check=False)

    def test_head_office_for_returns_existing(self):
        self.assertEqual(Department.head_office_for(self.mosaic), self.head)
        self.assertEqual(Department.objects.filter(insurance_company=self.mosaic).count(), 1)

    def test_cannot_be_closed(self):
        self.head.status = Department.Status.CLOSED
        self.assertIn("status", self.errors(self.head))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.head.save()

    def test_region_from_company(self):
        self.assertEqual(self.head.effective_region().pk, TASHKENT)


class StructureTests(DepartmentCase):
    def test_division_in_head_office_and_branch(self):
        division = self.make(name="Бухгалтерия")
        branch = self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак", region_id=JIZZAKH)
        in_branch = self.make(name="Продажи", parent=branch)
        self.assertEqual(division.full_name(), "Головной офис › Бухгалтерия")
        self.assertEqual(in_branch.full_name(), "Филиал Джизак › Продажи")
        # Регион отдела - от того, во что он входит.
        self.assertEqual(division.effective_region().pk, TASHKENT)
        self.assertEqual(in_branch.effective_region().pk, JIZZAKH)

    def test_division_needs_parent(self):
        self.assertIn("parent", self.errors(Department(insurance_company=self.mosaic, kind="division", name="Отдел")))
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make(parent=None, check=False)

    def test_branch_has_no_parent(self):
        branch = Department(insurance_company=self.mosaic, kind="branch", name="Филиал", region_id=JIZZAKH, parent=self.head)
        self.assertIn("parent", self.errors(branch))
        with self.assertRaises(IntegrityError), transaction.atomic():
            branch.save()

    def test_no_division_inside_division(self):
        division = self.make(name="Продажи")
        nested = Department(insurance_company=self.mosaic, kind="division", name="Группа", parent=division)
        self.assertIn("другой отдел", self.errors(nested)["parent"][0])
        with self.assertRaises(IntegrityError), transaction.atomic():
            nested.save()  # база тоже не пропускает третий уровень

    def test_parent_of_other_company_rejected(self):
        stranger = Department(insurance_company=self.mosaic, kind="division", name="Отдел", parent=self.other_head)
        self.assertIn("другой страховой", self.errors(stranger)["parent"][0])
        with self.assertRaises(IntegrityError), transaction.atomic():
            stranger.save()

    def test_branch_needs_region(self):
        branch = Department(insurance_company=self.mosaic, kind="branch", name="Филиал")
        self.assertIn("region", self.errors(branch))
        with self.assertRaises(IntegrityError), transaction.atomic():
            branch.save()

    def test_region_only_for_branch(self):
        division = Department(insurance_company=self.mosaic, kind="division", name="Отдел", parent=self.head, region_id=FERGANA)
        self.assertIn("region", self.errors(division))
        with self.assertRaises(IntegrityError), transaction.atomic():
            division.save()


class CodeAndNameTests(DepartmentCase):
    def test_code_spaces_removed(self):
        self.assertEqual(self.make(code=" 01 01 ").code, "0101")

    def test_code_format(self):
        for bad in ["1", "1234567", "12a4"]:
            with self.subTest(code=bad):
                department = Department(insurance_company=self.mosaic, kind="division", name="Отдел", parent=self.head, code=bad)
                self.assertIn("code", self.errors(department))
        with self.assertRaises(IntegrityError), transaction.atomic():
            # _base_manager - намеренный обход кода, чтобы проверить базу.
            Department._base_manager.filter(pk=self.head.pk).update(code="12a")

    def test_code_unique_in_company(self):
        self.make(name="Бухгалтерия", code="0101")
        duplicate = Department(insurance_company=self.mosaic, kind="division", name="Продажи", parent=self.head, code="0101")
        self.assertIn("«Бухгалтерия»", self.errors(duplicate)["code"][0])
        with self.assertRaises(IntegrityError), transaction.atomic():
            duplicate.save()

    def test_same_code_in_other_company_allowed(self):
        self.make(name="Бухгалтерия", code="0101")
        self.make(name="Бухгалтерия", code="0101", company=self.other, parent=self.other_head)

    def test_many_departments_without_code(self):
        self.make(name="Бухгалтерия")
        self.make(name="Продажи")

    def test_name_unique_in_one_place_ignoring_case(self):
        self.make(name="Бухгалтерия")
        duplicate = Department(insurance_company=self.mosaic, kind="division", name="  бухгалтерия ", parent=self.head)
        self.assertIn("name", self.errors(duplicate))
        with self.assertRaises(IntegrityError), transaction.atomic():
            duplicate.save()

    def test_same_name_in_other_branch_allowed(self):
        branch = self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак")
        self.make(name="Бухгалтерия")
        self.make(name="Бухгалтерия", parent=branch)

    def test_closed_name_can_be_reused(self):
        old = self.make(name="Бухгалтерия")
        old.status = Department.Status.CLOSED
        old.full_clean()
        old.save()
        self.make(name="Бухгалтерия")

    def test_two_branches_with_same_name_rejected(self):
        # У филиалов «входит в» пусто - правило всё равно действует.
        self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак", check=False)


class FixedAfterCreationTests(DepartmentCase):
    def test_kind_cannot_change(self):
        division = self.make()
        division.kind = Department.Kind.BRANCH
        division.parent = None
        division.region_id = JIZZAKH
        self.assertIn("kind", self.errors(division))
        with self.assertRaises(IntegrityError), transaction.atomic():
            division.save()

    def test_parent_cannot_change(self):
        branch = self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак")
        division = self.make()
        division.parent = branch
        self.assertIn("parent", self.errors(division))
        with self.assertRaises(IntegrityError), transaction.atomic():
            division.save()

    def test_company_cannot_change(self):
        branch = self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Department._base_manager.filter(pk=branch.pk).update(insurance_company=self.other)

    def test_no_delete(self):
        with self.assertRaises(UntrackedChangeError):
            self.make().delete()


class ClosingTests(DepartmentCase):
    def test_branch_with_open_division_cannot_close(self):
        branch = self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак")
        self.make(name="Продажи", parent=branch)
        branch.status = Department.Status.CLOSED
        self.assertIn("Продажи", self.errors(branch)["status"][0])

    def test_branch_closes_after_its_divisions(self):
        branch = self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак")
        division = self.make(name="Продажи", parent=branch)
        for department in (division, branch):
            department.status = Department.Status.CLOSED
            department.full_clean()
            department.save()

    def test_no_new_division_in_closed_branch(self):
        branch = self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак")
        branch.status = Department.Status.CLOSED
        branch.save()
        division = Department(insurance_company=self.mosaic, kind="division", name="Продажи", parent=branch)
        self.assertIn("закрыто", self.errors(division)["parent"][0])


class DepartmentAdminTests(DepartmentCase):
    """Экран «Подразделения» от лица обычных сотрудников, не суперпользователя."""

    def login(self, group="Администратор страховой", company=None, username="admin_mosaic"):
        user = User.objects.create_user(
            username, password=STRONG_PASSWORD, is_staff=True,
            insurance_company=self.mosaic if company is None else (company or None),
        )
        # Место работы - всегда (без него не войти, A3); роль - если задана.
        give_roles(user, *([group] if group else []))
        self.client.force_login(user)
        return user

    def post_new(self, **data):
        return self.client.post(ADD_URL, {"kind": "division", "name": "Бухгалтерия", "code": "", **data})

    def test_list_shows_only_own_company(self):
        self.make(name="Бухгалтерия Mosaic")
        self.make(name="Бухгалтерия KZ", company=self.other, parent=self.other_head)
        self.login()
        page = self.client.get(LIST_URL)
        self.assertContains(page, "Бухгалтерия Mosaic")
        self.assertNotContains(page, "Бухгалтерия KZ")

    def test_other_company_department_not_opened(self):
        stranger = self.make(name="Бухгалтерия KZ", company=self.other, parent=self.other_head)
        self.login()
        page = self.client.get(f"{LIST_URL}{stranger.pk}/change/")
        self.assertEqual(page.status_code, 302)  # Django: «не найдено» -> на главную админ-панели
        self.assertNotContains(self.client.get(LIST_URL), "Бухгалтерия KZ")

    def test_create_division_gets_own_company(self):
        user = self.login()
        page = self.post_new(parent=str(self.head.pk), code="0101")
        self.assertEqual(page.status_code, 302, page.content.decode()[:2000])
        division = Department.objects.get(name="Бухгалтерия")
        self.assertEqual(division.insurance_company, self.mosaic)
        self.assertEqual(division.created_by, user)
        self.assertTrue(ChangeLog.objects.filter(object_id=division.pk, field="code", user=user, new_value="0101").exists())

    def test_form_offers_only_own_parents_and_no_head_office_kind(self):
        self.login()
        page = self.client.get(ADD_URL).content.decode()
        self.assertIn(f'value="{self.head.pk}"', page)
        self.assertNotIn(f'value="{self.other_head.pk}"', page)
        self.assertNotIn('value="head_office"', page)
        self.assertNotIn('name="insurance_company"', page)  # страховая - из учётной записи

    def test_other_company_parent_refused_with_message(self):
        self.login()
        page = self.post_new(parent=str(self.other_head.pk))
        self.assertEqual(page.status_code, 200)  # снова форма с ошибкой, не 500
        self.assertFalse(Department.objects.filter(name="Бухгалтерия").exists())

    def test_head_office_kind_refused(self):
        self.login()
        page = self.post_new(kind="head_office", name="Второй офис")
        self.assertEqual(page.status_code, 200)
        self.assertFalse(Department.objects.filter(name="Второй офис").exists())

    def test_duplicate_code_shows_message(self):
        self.make(name="Продажи", code="0101")
        self.login()
        page = self.post_new(parent=str(self.head.pk), code="0101")
        self.assertContains(page, "уже у подразделения «Продажи»")

    def test_duplicate_name_shows_message(self):
        self.make(name="Бухгалтерия")
        self.login()
        page = self.post_new(parent=str(self.head.pk), name="бухгалтерия")
        self.assertContains(page, "с таким названием здесь уже есть")

    def test_branch_without_region_shows_message(self):
        self.login()
        page = self.post_new(kind="branch", name="Филиал Джизак")
        self.assertContains(page, "Укажите регион филиала")

    def test_create_branch(self):
        self.login()
        page = self.post_new(kind="branch", name="Филиал Джизак", region=str(JIZZAKH), code="02")
        self.assertEqual(page.status_code, 302, page.content.decode()[:2000])
        self.assertEqual(Department.objects.get(name="Филиал Джизак").region_id, JIZZAKH)

    def test_change_keeps_kind_and_parent(self):
        branch = self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак")
        division = self.make(name="Продажи")
        self.login()
        page = self.client.post(
            f"{LIST_URL}{division.pk}/change/",
            {"name": "Продажи и андеррайтинг", "code": "07", "status": "active",
             "kind": "branch", "parent": str(branch.pk)},  # поля только для чтения - игнорируются
        )
        self.assertEqual(page.status_code, 302, page.content.decode()[:2000])
        division.refresh_from_db()
        self.assertEqual((division.name, division.code), ("Продажи и андеррайтинг", "07"))
        self.assertEqual((division.kind, division.parent_id), ("division", self.head.pk))

    def test_closing_branch_with_open_division_shows_message(self):
        branch = self.make(kind=Department.Kind.BRANCH, name="Филиал Джизак")
        self.make(name="Продажи", parent=branch)
        self.login()
        page = self.client.post(
            f"{LIST_URL}{branch.pk}/change/",
            {"name": branch.name, "code": "", "region": str(JIZZAKH), "status": "closed"},
        )
        self.assertContains(page, "Сначала закройте отделы в нём")

    def test_no_delete(self):
        division = self.make()
        self.login()
        with refusal_expected(self):
            page = self.client.get(f"{LIST_URL}{division.pk}/delete/")
        self.assertEqual(page.status_code, 403)

    def test_employee_views_but_cannot_add(self):
        self.make(name="Бухгалтерия")
        self.login(group="Сотрудник", username="clerk")
        self.assertContains(self.client.get(LIST_URL), "Бухгалтерия")
        with refusal_expected(self):
            page = self.client.get(ADD_URL)
        self.assertEqual(page.status_code, 403)

    def test_user_without_company_sees_nothing(self):
        # Будущий администратор платформы: без страховой и не суперпользователь.
        self.make(name="Бухгалтерия")
        self.login(company="", username="platform")
        page = self.client.get(LIST_URL)
        self.assertNotContains(page, "Бухгалтерия")

    def test_superuser_sees_all_and_chooses_company(self):
        self.make(name="Бухгалтерия KZ", company=self.other, parent=self.other_head)
        admin_user = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        self.client.force_login(admin_user)
        self.assertContains(self.client.get(LIST_URL), "Бухгалтерия KZ")
        self.assertContains(self.client.get(ADD_URL), 'name="insurance_company"')


class CompanyAdminCreatesHeadOfficeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_from_files()

    def test_new_company_in_admin_gets_head_office(self):
        self.client.force_login(User.objects.create_superuser("root", password=STRONG_PASSWORD))
        page = self.client.post(
            "/admin/companies/insurancecompany/add/",
            {"name": "Inson", "code": "inson", "country": "UZ", "ersp_insurer": "1084",
             "ersp_region": str(TASHKENT), "is_active": "on"},
        )
        self.assertEqual(page.status_code, 302, page.content.decode()[:2000])
        company = InsuranceCompany.objects.get(code="inson")
        self.assertEqual(
            list(company.departments.values_list("kind", "name")),
            [("head_office", "Головной офис")],
        )
