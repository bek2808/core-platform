"""
Автотесты администратора платформы (часть A4, docs/users-model.md).

Всё - от лица обычного администратора платформы (вид «платформа», без
страховой, не суперпользователь; права - группой «Администратор платформы»).

Главное, что проверяется:
- экран страховых: счётчики, ссылка на пользователей, журнал карточки;
- «Завести администратора»: место - головной офис, роль ставится сама,
  временный пароль один раз;
- пользователи страховых - вариант «Б»: список всех (без суперпользователей),
  карточка - только у администраторов страховых; место и роли не меняет;
- данных страховых не видит (контрагенты, журнал изменений их записей);
- администраторов платформы заводит только суперпользователь;
- выключенная страховая: её сотрудники не входят, сессии перестают действовать.

Запуск на сервере: bin/test accounts
"""
from django.contrib.auth import authenticate
from django.test import TestCase
from django.utils import timezone

from audit.models import ChangeLog
from companies.models import Department, InsuranceCompany
from config.testing import refusal_expected

from . import personnel
from .models import JobPosition, Role, User, current_assignment
from .testing import give_roles

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
PLATFORM_GROUP = "Администратор платформы"
COMPANIES = "/admin/companies/insurancecompany/"
USERS = "/admin/accounts/user/"


def card(user):
    return f"{USERS}{user.pk}/change/"


class PlatformCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mosaic = InsuranceCompany.objects.create(name="Mosaic", code="mosaic", country="KZ")
        cls.other = InsuranceCompany.objects.create(name="Other", code="other", country="KZ")
        Department(insurance_company=cls.mosaic, kind="division", name="Бухгалтерия",
                   parent=Department.head_office_for(cls.mosaic)).save()
        cls.platform = User.objects.create_user("bek2808", password=STRONG_PASSWORD, kind=User.Kind.PLATFORM)
        give_roles(cls.platform, PLATFORM_GROUP)
        cls.mosaic_admin = cls.make("mosaic_admin", cls.mosaic, "Администратор страховой")
        cls.ivanov = cls.make("ivanov", cls.mosaic, "Сотрудник", phone="998901234567")
        cls.stranger = cls.make("stranger", cls.other, "Сотрудник")
        cls.blocked = cls.make("blocked", cls.mosaic, "Сотрудник")
        cls.blocked.is_active = False
        cls.blocked.save()
        cls.hidden_root = User.objects.create_superuser("mosaic_root", password=STRONG_PASSWORD,
                                                        insurance_company=cls.mosaic)

    @classmethod
    def make(cls, username, company, role, **fields):
        user = User.objects.create_user(username, password=STRONG_PASSWORD, insurance_company=company, **fields)
        give_roles(user, role)
        return user

    def setUp(self):
        self.client.force_login(self.platform)


class CompaniesScreenTests(PlatformCase):
    def test_counters_and_users_link(self):
        page = self.client.get(COMPANIES)
        self.assertEqual(page.status_code, 200)
        mosaic = next(c for c in page.context["cl"].result_list if c.pk == self.mosaic.pk)
        self.assertEqual((mosaic.working, mosaic.blocked, mosaic.departments_total), (2, 1, 2))
        self.assertContains(page, f"?insurance_company__id__exact={self.mosaic.pk}")
        self.assertContains(page, ">2 / 1</a>")

    def test_new_company_with_head_office_and_journal(self):
        page = self.client.post(f"{COMPANIES}add/", {"name": "Gross", "code": "gross", "country": "KZ",
                                                     "is_active": "on"})
        self.assertEqual(page.status_code, 302, page.content.decode()[:2000])
        company = InsuranceCompany.objects.get(code="gross")
        self.assertEqual(company.created_by, self.platform)
        self.assertTrue(Department.objects.filter(insurance_company=company, kind="head_office").exists())
        self.assertEqual(ChangeLog.objects.get(field="name", new_value="Gross").user, self.platform)

    def test_switch_off_is_logged(self):
        page = self.client.post(f"{COMPANIES}{self.other.pk}/change/", {"name": "Other", "country": "KZ"})
        self.assertEqual(page.status_code, 302, page.content.decode()[:2000])
        entry = ChangeLog.objects.filter(field="is_active").latest("pk")
        self.assertEqual((entry.user, entry.new_value), (self.platform, False))

    def test_company_admin_has_no_companies_screen(self):
        self.client.force_login(self.mosaic_admin)
        with refusal_expected(self):
            self.assertEqual(self.client.get(COMPANIES).status_code, 403)


class AddCompanyAdminTests(PlatformCase):
    def url(self, company=None):
        return f"{COMPANIES}{(company or self.other).pk}/add-admin/"

    def test_button_on_company_card(self):
        self.assertContains(self.client.get(f"{COMPANIES}{self.other.pk}/change/"), "Завести администратора")

    def test_admin_created_with_place_role_and_password(self):
        page = self.client.post(self.url(), {
            "username": "other_admin", "last_name": "Каримов", "first_name": "Акмал",
            "phone": "+998 90 765-43-21", "position": JobPosition.objects.get(name="Бизнес-аналитик").pk,
            "valid_from": timezone.localdate().strftime("%d.%m.%Y"),
        })
        self.assertEqual(page.status_code, 200)
        password = page.context["password"]
        user = User.objects.get(username="other_admin")
        self.assertEqual((user.insurance_company, user.kind, user.phone), (self.other, "employee", "998907654321"))
        self.assertTrue(user.must_change_password and user.check_password(password))
        self.assertEqual(user.created_by, self.platform)
        place = current_assignment(user)
        self.assertEqual(place.department, Department.head_office_for(self.other))
        self.assertEqual([r.code for r in place.roles.all()], ["company_admin"])
        self.assertTrue(User.objects.get(pk=user.pk).has_perm("accounts.change_user"))

    def test_names_required(self):
        page = self.client.post(self.url(), {"username": "x_admin", "valid_from": "01.10.2026"})
        self.assertEqual(page.status_code, 200)
        self.assertFalse(User.objects.filter(username="x_admin").exists())
        self.assertIn("last_name", page.context["form"].errors)

    def test_not_for_switched_off_company(self):
        self.other.is_active = False
        self.other.save()
        with refusal_expected(self):
            self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_company_admin_cannot(self):
        self.client.force_login(self.mosaic_admin)
        with refusal_expected(self):
            self.assertEqual(self.client.get(self.url(self.mosaic)).status_code, 403)


class PlatformUsersTests(PlatformCase):
    def test_list_all_companies_without_superusers(self):
        page = self.client.get(USERS)
        names = {u.username for u in page.context["cl"].result_list}
        self.assertEqual(names, {"bek2808", "mosaic_admin", "ivanov", "stranger", "blocked"})
        self.assertNotContains(page, "работает сейчас")  # место работы людей страховой - не платформе

    def test_card_only_for_company_admins(self):
        self.assertEqual(self.client.get(card(self.mosaic_admin)).status_code, 200)
        for user in (self.ivanov, self.stranger):
            with refusal_expected(self):
                self.assertEqual(self.client.get(card(user)).status_code, 403)
        page = self.client.get(USERS).content.decode()
        self.assertIn(f'href="{card(self.mosaic_admin)}"', page)
        self.assertNotIn(f'href="{card(self.ivanov)}"', page)
        self.assertNotIn("998901234567", page)

    def test_admin_card_without_place_and_role_controls(self):
        page = self.client.get(card(self.mosaic_admin)).content.decode()
        self.assertIn("Сбросить пароль", page)
        self.assertIn('name="is_active"', page)
        for absent in ("/transfer/", "/dismiss/", "/hire/", "assignments-TOTAL_FORMS", 'name="groups"',
                       'name="is_superuser"'):
            self.assertNotIn(absent, page)

    def test_reset_admin_password(self):
        page = self.client.post(f"{USERS}{self.mosaic_admin.pk}/reset-password/")
        self.assertTrue(User.objects.get(pk=self.mosaic_admin.pk).check_password(page.context["password"]))
        self.assertEqual(ChangeLog.objects.filter(field="password").latest("pk").user, self.platform)

    def test_place_actions_refused(self):
        for action in ("transfer", "dismiss", "hire"):
            with refusal_expected(self):
                self.assertEqual(self.client.get(f"{USERS}{self.mosaic_admin.pk}/{action}/").status_code, 403)

    def test_cannot_block_sole_admin(self):
        page = self.client.post(card(self.mosaic_admin), {"username": "mosaic_admin", "kind": "employee"})
        self.assertContains(page, "не останется администратора")
        self.assertTrue(User.objects.get(pk=self.mosaic_admin.pk).is_active)

    def test_blocks_admin_when_another_exists(self):
        self.make("mosaic_admin2", self.mosaic, "Администратор страховой")
        page = self.client.post(card(self.mosaic_admin), {"username": "mosaic_admin", "kind": "employee"})
        self.assertEqual(page.status_code, 302, page.content.decode()[:2000])
        self.assertFalse(User.objects.get(pk=self.mosaic_admin.pk).is_active)

    def test_no_generic_add(self):
        with refusal_expected(self):
            self.assertEqual(self.client.get(f"{USERS}add/").status_code, 403)

    def test_platform_colleague_view_only(self):
        colleague = User.objects.create_user("colleague", password=STRONG_PASSWORD, kind=User.Kind.PLATFORM)
        page = self.client.get(card(colleague)).content.decode()
        self.assertNotIn('name="is_active"', page)
        self.assertNotIn('name="last_name"', page)

    def test_role_filter(self):
        page = self.client.get(f"{USERS}?role=company_admin")
        self.assertEqual([u.username for u in page.context["cl"].result_list], ["mosaic_admin"])
        page = self.client.get(f"{USERS}?role=platform_admin")
        self.assertEqual([u.username for u in page.context["cl"].result_list], ["bek2808"])


class PlatformBoundariesTests(PlatformCase):
    def test_no_insurer_data(self):
        for url in ("/admin/counterparties/counterparty/", "/admin/integrations/exchange/",
                    "/admin/companies/department/"):
            with refusal_expected(self):
                self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_journal_only_platform_tables(self):
        personnel.issue_temporary_password(self.ivanov, by=self.mosaic_admin)
        self.client.post(f"{COMPANIES}add/", {"name": "Gross", "code": "gross", "country": "KZ", "is_active": "on"})
        page = self.client.get("/admin/audit/changelog/")
        tables = {entry.content_type.model for entry in page.context["cl"].result_list}
        self.assertIn("insurancecompany", tables)
        self.assertEqual(tables - {"insurancecompany", "jobposition"}, set())

    def test_only_superuser_makes_platform_admins(self):
        page = self.client.get(card(self.platform)).content.decode()
        self.assertNotIn('name="groups"', page)
        self.assertNotIn('value="platform"', page)
        root = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        self.client.force_login(root)
        page = self.client.post(f"{USERS}add/", {"username": "platform2", "kind": "platform", "visibility": "company"})
        self.assertEqual(page.status_code, 200, page.content.decode()[:2000])
        self.assertIn("password", page.context)
        user = User.objects.get(username="platform2")
        self.assertEqual((user.kind, user.insurance_company), ("platform", None))
        self.assertContains(self.client.get(card(user)), 'name="groups"')

    def test_job_positions_renamed_only_by_platform(self):
        position = JobPosition.objects.get(name="Ассистент")
        url = f"/admin/accounts/jobposition/{position.pk}/change/"
        page = self.client.post(url, {"name": "Ассистент менеджера", "sort_order": position.sort_order,
                                      "is_active": "on"})
        self.assertEqual(page.status_code, 302, page.content.decode()[:2000])
        self.client.force_login(self.mosaic_admin)
        self.assertNotContains(self.client.get(url), 'name="name"')  # только просмотр


class SwitchedOffCompanyTests(PlatformCase):
    def test_staff_cannot_log_in_and_sessions_end(self):
        self.assertIsNotNone(authenticate(username="ivanov", password=STRONG_PASSWORD))
        self.client.force_login(self.ivanov)
        self.mosaic.is_active = False
        self.mosaic.save(by=self.platform)
        self.assertIsNone(authenticate(username="ivanov", password=STRONG_PASSWORD))
        self.assertRedirects(self.client.get("/admin/"), "/admin/login/?next=/admin/", fetch_redirect_response=False)
        self.mosaic.is_active = True
        self.mosaic.save(by=self.platform)
        self.assertIsNotNone(authenticate(username="ivanov", password=STRONG_PASSWORD))

    def test_platform_unaffected(self):
        self.mosaic.is_active = False
        self.mosaic.save()
        self.assertIsNotNone(authenticate(username="bek2808", password=STRONG_PASSWORD))

    def test_platform_role_in_catalog(self):
        role = Role.objects.get(code="platform_admin")
        self.assertEqual((role.audience, str(role)), ("platform", PLATFORM_GROUP))
