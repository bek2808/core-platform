"""
Автотесты связи подразделений с ЕРСП (agents.ErspLink, часть B1;
модель: docs/users-model.md, «Связь с ЕРСП: agencyId»).

Главное, что проверяется:
- страховая и среда связи берутся из записи ЕРСП;
- отдел не связывается (в ЕРСП отделов нет) и берёт agencyId своего
  головного офиса или филиала;
- одна запись ЕРСП - одна связь; у подразделения в среде - одна
  действующая связь; удалённую или пропавшую в ЕРСП запись связать нельзя;
- связь не переносится и не удаляется (только снимается отметка);
- каждое правило держит и база (триггер), а не только форма;
- экран подразделения: блок «Связь с ЕРСП» у головного офиса и филиала,
  только записи своей страховой, изменения - в «Истории» подразделения.

Запуск на сервере: bin/test agents
"""
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase

from accounts.models import User
from accounts.testing import give_roles
from audit.models import ChangeLog, UntrackedChangeError
from companies.models import Department, InsuranceCompany
from integrations.models import Environment, ErspAgency
from references.loader import load_from_files

from .models import ErspLink, agency_id_for_department

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
TASHKENT, JIZZAKH = 10, 13
DEPARTMENTS_URL = "/admin/companies/department/"


def agency(company, ersp_id, environment=Environment.SANDBOX, **fields):
    """Запись справочника ЕРСП, как после загрузки load_ersp_agencies."""
    from django.utils import timezone

    defaults = {"name": f"Запись {ersp_id}", "agency_type": ErspAgency.AgencyType.BRANCH,
                "raw": {"id": ersp_id}, "synced_at": timezone.now()}
    return ErspAgency.objects.create(
        insurance_company=company, environment=environment, ersp_id=ersp_id, **{**defaults, **fields}
    )


class LinkCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_from_files()  # справочники ЕРСП - один раз на группу тестов
        cls.mosaic = InsuranceCompany.objects.create(
            name="Mosaic Insurance Group", code="mosaic", ersp_insurer_id=1084, ersp_region_id=TASHKENT
        )
        cls.other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")
        cls.head = Department.head_office_for(cls.mosaic)
        cls.branch = Department(insurance_company=cls.mosaic, kind="branch", name="Филиал Джизак", region_id=JIZZAKH)
        cls.branch.save()
        cls.division = Department(insurance_company=cls.mosaic, kind="division", name="Бухгалтерия", parent=cls.head)
        cls.division.save()
        cls.branch_division = Department(
            insurance_company=cls.mosaic, kind="division", name="Продажи", parent=cls.branch
        )
        cls.branch_division.save()
        # Песочница Mosaic: 53 «direct dep» действует, 1084 «test» удалён (снимок экрана ЕРСП 21.10).
        cls.direct_dep = agency(cls.mosaic, 53, name="direct dep")
        cls.deleted = agency(cls.mosaic, 1084, name="test", is_deleted=True)
        cls.prod_head = agency(cls.mosaic, 1600, Environment.PROD, name="Головной Офис")
        cls.sandbox_branch = agency(cls.mosaic, 60, name="Филиал Джизак")
        cls.foreign = agency(cls.other, 77, name="Чужой филиал")

    def link(self, department, ersp_agency, check=True, **fields):
        """Связать так же, как экран: сначала проверка, потом сохранение."""
        link = ErspLink(department=department, ersp_agency=ersp_agency, **fields)
        if check:
            link.full_clean()
        link.save()
        return link

    def errors(self, link):
        with self.assertRaises(ValidationError) as caught:
            link.full_clean()
        return caught.exception.message_dict


class LinkRulesTests(LinkCase):
    def test_company_and_environment_from_ersp_record(self):
        link = self.link(self.head, self.direct_dep)
        self.assertEqual((link.insurance_company, link.environment), (self.mosaic, "sandbox"))

    def test_division_not_linked(self):
        errors = self.errors(ErspLink(department=self.division, ersp_agency=self.direct_dep))
        self.assertIn("в ЕРСП отделов нет", errors["department"][0])

    def test_other_company_record_refused(self):
        errors = self.errors(ErspLink(department=self.head, ersp_agency=self.foreign))
        self.assertIn("другой страховой", errors["ersp_agency"][0])

    def test_deleted_or_absent_record_refused(self):
        self.assertIn("удалена в ЕРСП", self.errors(ErspLink(department=self.head, ersp_agency=self.deleted))["ersp_agency"][0])
        absent = agency(self.mosaic, 61, is_present=False)
        self.assertIn("нет в последней загрузке", self.errors(ErspLink(department=self.head, ersp_agency=absent))["ersp_agency"][0])

    def test_one_active_link_per_environment(self):
        self.link(self.head, self.direct_dep)
        errors = self.errors(ErspLink(department=self.head, ersp_agency=self.sandbox_branch))
        self.assertIn("уже есть действующая связь - 53", errors["ersp_agency"][0])
        # В другой среде - своя связь.
        self.link(self.head, self.prod_head)
        self.assertEqual(self.head.ersp_links.count(), 2)

    def test_one_link_per_ersp_record(self):
        self.link(self.head, self.direct_dep)
        self.assertIn("ersp_agency", self.errors(ErspLink(department=self.branch, ersp_agency=self.direct_dep)))

    def test_link_not_moved(self):
        link = self.link(self.head, self.direct_dep)
        link.ersp_agency = self.sandbox_branch
        self.assertIn("не переносится", self.errors(link)["ersp_agency"][0])

    def test_deactivate_then_new_link(self):
        # Запись в ЕРСП заменили: старую связь снимают, новую заводят.
        old = self.link(self.head, self.direct_dep)
        old.is_active = False
        old.full_clean()
        old.save()
        self.link(self.head, self.sandbox_branch)
        self.assertEqual(agency_id_for_department(self.head, "sandbox"), 60)

    def test_reactivating_link_to_deleted_record_refused(self):
        link = self.link(self.head, self.direct_dep, is_active=False)
        self.direct_dep.is_deleted = True
        self.direct_dep.save()
        link.is_active = True
        self.assertIn("удалена в ЕРСП", self.errors(link)["ersp_agency"][0])
        self.direct_dep.is_deleted = False
        self.direct_dep.save()

    def test_no_delete(self):
        link = self.link(self.head, self.direct_dep)
        with self.assertRaises(UntrackedChangeError):
            link.delete()


class AgencyIdTests(LinkCase):
    """Какой agencyId уйдёт с продажей сотрудника (для модуля договоров)."""

    def test_head_office_branch_and_divisions(self):
        self.link(self.head, self.direct_dep)
        self.link(self.branch, self.sandbox_branch)
        self.assertEqual(agency_id_for_department(self.head, "sandbox"), 53)
        self.assertEqual(agency_id_for_department(self.division, "sandbox"), 53)  # отдел головного офиса
        self.assertEqual(agency_id_for_department(self.branch, "sandbox"), 60)
        self.assertEqual(agency_id_for_department(self.branch_division, "sandbox"), 60)  # отдел филиала

    def test_by_environment(self):
        self.link(self.head, self.direct_dep)
        self.assertIsNone(agency_id_for_department(self.head, "prod"))
        self.link(self.head, self.prod_head)
        self.assertEqual(agency_id_for_department(self.head, "prod"), 1600)

    def test_none_when_unlinked_inactive_or_deleted(self):
        self.assertIsNone(agency_id_for_department(self.head, "sandbox"))
        link = self.link(self.head, self.direct_dep)
        self.direct_dep.is_deleted = True  # удалили в ЕРСП уже после связи
        self.direct_dep.save()
        self.assertIsNone(agency_id_for_department(self.head, "sandbox"))
        self.direct_dep.is_deleted = False
        self.direct_dep.save()
        link.is_active = False
        link.save()
        self.assertIsNone(agency_id_for_department(self.head, "sandbox"))


class DatabaseRulesTests(LinkCase):
    """Правила держит база, даже если записать в обход проверок."""

    def assert_refused(self, action):
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            action()

    def test_division_refused_by_database(self):
        self.assert_refused(lambda: self.link(self.division, self.direct_dep, check=False))

    def test_other_company_record_refused_by_database(self):
        self.assert_refused(lambda: self.link(self.head, self.foreign, check=False))

    def test_company_or_environment_mismatch_refused(self):
        def forged():
            link = ErspLink(department=self.head, ersp_agency=self.direct_dep)
            link.normalize()
            link.environment = "prod"
            # save() снова возьмёт среду из записи ЕРСП - пишем мимо него.
            ErspLink._base_manager.bulk_create([link])
        self.assert_refused(forged)

    def test_second_active_link_refused_by_database(self):
        self.link(self.head, self.direct_dep)
        self.assert_refused(lambda: self.link(self.head, self.sandbox_branch, check=False))

    def test_link_cannot_be_moved_in_database(self):
        link = self.link(self.head, self.direct_dep)
        self.assert_refused(
            lambda: ErspLink._base_manager.filter(pk=link.pk).update(ersp_agency=self.sandbox_branch)
        )
        self.assert_refused(lambda: ErspLink._base_manager.filter(pk=link.pk).update(department=self.branch))


class DepartmentScreenTests(LinkCase):
    """Блок «Связь с ЕРСП» в карточке подразделения - от лица администратора страховой."""

    def login(self, *groups, username="admin_mosaic"):
        user = User.objects.create_user(username, password=STRONG_PASSWORD, insurance_company=self.mosaic)
        give_roles(user, *groups)
        self.client.force_login(user)
        return user

    def change_url(self, department):
        return f"{DEPARTMENTS_URL}{department.pk}/change/"

    def post_head_office(self, rows, initial=0):
        data = {
            "name": self.head.name, "code": "", "status": "active",
            "ersp_links-TOTAL_FORMS": str(len(rows)), "ersp_links-INITIAL_FORMS": str(initial),
            "ersp_links-MIN_NUM_FORMS": "0", "ersp_links-MAX_NUM_FORMS": "1000",
        }
        for i, row in enumerate(rows):
            data.update({f"ersp_links-{i}-{key}": value for key, value in row.items()})
            data[f"ersp_links-{i}-department"] = str(self.head.pk)
        return self.client.post(self.change_url(self.head), data)

    def test_block_on_head_office_and_branch_not_division(self):
        self.login("Администратор страховой")
        self.assertContains(self.client.get(self.change_url(self.head)), "Связь с ЕРСП")
        self.assertContains(self.client.get(self.change_url(self.branch)), "Связь с ЕРСП")
        self.assertNotContains(self.client.get(self.change_url(self.division)), "Связь с ЕРСП")

    def test_choices_only_live_own_unlinked(self):
        self.login("Администратор страховой")
        self.link(self.branch, self.sandbox_branch)
        page = self.client.get(self.change_url(self.head)).content.decode()
        self.assertIn(f'value="{self.direct_dep.pk}"', page)
        self.assertIn("53 - direct dep (филиал, песочница)", page)
        self.assertNotIn(f'value="{self.deleted.pk}"', page)         # удалена в ЕРСП
        self.assertNotIn(f'value="{self.foreign.pk}"', page)         # чужая страховая
        self.assertNotIn(f'value="{self.sandbox_branch.pk}"', page)  # уже связана с филиалом

    def test_add_link_written_to_department_history(self):
        user = self.login("Администратор страховой")
        page = self.post_head_office([{"ersp_agency": str(self.direct_dep.pk), "is_active": "on"}])
        self.assertEqual(page.status_code, 302, page.content.decode()[:3000])
        link = ErspLink.objects.get(department=self.head)
        self.assertEqual((link.ersp_agency, link.environment, link.created_by), (self.direct_dep, "sandbox", user))
        self.assertTrue(ChangeLog.objects.filter(object_id=link.pk, field="ersp_agency", user=user).exists())
        history = self.client.get(f"{DEPARTMENTS_URL}{self.head.pk}/history/", follow=True)
        self.assertContains(history, "запись ЕРСП")

    def test_deactivate_on_screen(self):
        self.login("Администратор страховой")
        link = self.link(self.head, self.direct_dep)
        page = self.post_head_office(
            [{"id": str(link.pk), "ersp_agency": str(self.sandbox_branch.pk)}], initial=1,  # «действует» снята
        )
        self.assertEqual(page.status_code, 302, page.content.decode()[:3000])
        link.refresh_from_db()
        self.assertFalse(link.is_active)
        self.assertEqual(link.ersp_agency, self.direct_dep)  # запись ЕРСП на экране не меняется

    def test_second_active_link_shows_message(self):
        self.login("Администратор страховой")
        self.link(self.head, self.direct_dep)
        link = ErspLink.objects.get(department=self.head)
        page = self.post_head_office([
            {"id": str(link.pk), "ersp_agency": str(self.direct_dep.pk), "is_active": "on"},
            {"ersp_agency": str(self.sandbox_branch.pk), "is_active": "on"},
        ], initial=1)
        self.assertContains(page, "уже есть действующая связь")
        self.assertEqual(ErspLink.objects.filter(department=self.head).count(), 1)

    def test_employee_without_rights_sees_no_block(self):
        self.login("Сотрудник", username="clerk")
        page = self.client.get(self.change_url(self.head))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "Связь с ЕРСП")
