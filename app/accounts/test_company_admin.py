"""
Автотесты администратора страховой (часть A3, docs/users-model.md).

Всё - от лица обычного администратора страховой (не суперпользователя):
суперпользователь проходит любые проверки прав.

Главное, что проверяется:
- видит и заводит только пользователей своей страховой, суперпользователей
  не видит; роль платформы, группы, «суперпользователь» ему недоступны;
- новый пользователь - одним экраном, сразу с местом работы; пароль выдаёт
  система (показывает один раз), при первом входе - обязательная смена;
- «Принять», «Перевести», «Уволить», «Сбросить пароль» - и запись в журнал
  от имени администратора;
- свою карточку меняет частично: роли, место, блокировку - нет;
- без действующего прикрепления сотрудник страховой не входит;
- «последний администратор»: блокировка, увольнение, смена ролей не оставляют
  страховую без администратора - ни сегодня, ни в будущем.

Запуск на сервере: bin/test accounts
"""
import datetime
import re

from django.contrib.auth import authenticate
from django.test import TestCase
from django.utils import timezone

from audit.models import ChangeLog
from companies.models import Department, InsuranceCompany
from config.testing import refusal_expected

from . import personnel
from .models import Assignment, JobPosition, Role, User, current_assignment
from .testing import LONG_AGO, give_roles

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
ADMIN = "Администратор страховой"
USERS = "/admin/accounts/user/"
DAY = datetime.timedelta(days=1)


def today():
    return timezone.localdate()


def card(user):
    return f"{USERS}{user.pk}/change/"


class CompanyAdminCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mosaic = InsuranceCompany.objects.create(name="Mosaic", code="mosaic", country="KZ")
        cls.other = InsuranceCompany.objects.create(name="Other", code="other", country="KZ")
        cls.head_office = Department.head_office_for(cls.mosaic)
        cls.other_office = Department.head_office_for(cls.other)
        cls.accounting = Department(insurance_company=cls.mosaic, kind="division", name="Бухгалтерия",
                                    parent=cls.head_office)
        cls.accounting.save()
        cls.analyst = JobPosition.objects.get(name="Бизнес-аналитик")
        cls.accountant = JobPosition.objects.get(name="Главный бухгалтер")
        cls.admin_role = Role.objects.get(code="company_admin")
        cls.employee_role = Role.objects.get(code="employee")
        cls.references_role = Role.objects.get(code="references_admin")

        cls.admin = cls.make("admin")
        give_roles(cls.admin, ADMIN)
        cls.ivanov = cls.make("ivanov", last_name="Иванов")
        give_roles(cls.ivanov, "Сотрудник")
        cls.stranger = cls.make("stranger", company=cls.other)
        cls.hidden_root = User.objects.create_superuser("mosaic_root", password=STRONG_PASSWORD,
                                                        insurance_company=cls.mosaic)

    @classmethod
    def make(cls, username, company=None, **fields):
        return User.objects.create_user(username, password=STRONG_PASSWORD,
                                        insurance_company=company or cls.mosaic, **fields)

    def setUp(self):
        self.client.force_login(self.admin)

    def place(self, user):
        return current_assignment(user)

    def post_card(self, user, **changes):
        """Отправить карточку пользователя как есть, поменяв отдельные поля."""
        page = self.client.get(card(user))
        form = page.context["adminform"].form
        data = {}
        for name, field in form.fields.items():
            value = form.initial.get(name, field.initial)
            if value is None or value is False:
                continue
            data[name] = [str(v.pk) for v in value] if hasattr(value, "__iter__") and not isinstance(value, str) \
                else value
        for formset in page.context["inline_admin_formsets"]:
            management = formset.formset.management_form
            prefix = formset.formset.prefix
            for name in ("TOTAL_FORMS", "INITIAL_FORMS", "MIN_NUM_FORMS", "MAX_NUM_FORMS"):
                data[f"{prefix}-{name}"] = management.initial.get(name, management[name].value())
            for index, inline_form in enumerate(formset.formset.forms):
                for name, field in inline_form.fields.items():
                    value = inline_form.initial.get(name, field.initial)
                    key = f"{prefix}-{index}-{name}"
                    if name == "roles":
                        data[key] = [str(getattr(r, "pk", r)) for r in (value or [])]
                    elif value not in (None, False, ""):
                        data[key] = getattr(value, "pk", value)
        data.update(changes)
        return self.client.post(card(user), {k: v for k, v in data.items() if v is not None})


class VisibilityTests(CompanyAdminCase):
    def test_list_only_own_company_without_superusers(self):
        page = self.client.get(USERS)
        self.assertEqual(page.status_code, 200)
        names = {u.username for u in page.context["cl"].result_list}
        self.assertEqual(names, {"admin", "ivanov"})

    def test_foreign_and_superuser_cards_closed(self):
        for user in (self.stranger, self.hidden_root):
            self.assertEqual(self.client.get(card(user)).status_code, 302)  # «не найден» - на главную
            self.assertEqual(self.client.get(f"{USERS}{user.pk}/reset-password/").status_code, 404)

    def test_no_privilege_fields(self):
        page = self.client.get(card(self.ivanov)).content.decode()
        for name in ("is_superuser", "groups", "user_permissions", "is_staff"):
            self.assertNotIn(f'name="{name}"', page)
        self.assertNotIn('value="platform"', page)

    def test_employee_role_has_no_users_screen(self):
        self.client.force_login(self.ivanov)
        with refusal_expected(self):
            self.assertEqual(self.client.get(USERS).status_code, 403)


class AddUserTests(CompanyAdminCase):
    def add(self, **fields):
        data = {
            "username": "petrova", "last_name": "Петрова", "first_name": "Анна", "kind": "employee",
            "valid_from": today().strftime("%d.%m.%Y"), "department": self.accounting.pk,
            "position": self.accountant.pk, "roles": [self.employee_role.pk], "visibility": "company",
            "basis": "Приказ 15-к",
        }
        data.update(fields)
        return self.client.post(f"{USERS}add/", {k: v for k, v in data.items() if v is not None})

    def test_one_screen_with_place_and_temporary_password(self):
        page = self.add()
        self.assertEqual(page.status_code, 200)
        password = page.context["password"]
        user = User.objects.get(username="petrova")
        self.assertEqual(user.insurance_company, self.mosaic)
        self.assertTrue(user.must_change_password)
        self.assertTrue(user.check_password(password))
        self.assertEqual(user.created_by, self.admin)
        place = self.place(user)
        self.assertEqual((place.department, place.position, place.basis), (self.accounting, self.accountant, "Приказ 15-к"))
        self.assertEqual(list(place.roles.all()), [self.employee_role])
        self.assertEqual(place.created_by, self.admin)
        self.assertNotIn(password, str(list(ChangeLog.objects.values_list("old_value", "new_value"))))

    def test_place_required(self):
        page = self.add(department=None, position=None)
        self.assertContains(page, "Укажите место работы")
        self.assertFalse(User.objects.filter(username="petrova").exists())

    def test_foreign_department_not_offered(self):
        page = self.add(department=self.other_office.pk)
        self.assertEqual(page.status_code, 200)
        self.assertFalse(User.objects.filter(username="petrova").exists())
        offered = self.client.get(f"{USERS}add/").context["adminform"].form.fields["department"].queryset
        self.assertIn(self.accounting, offered)
        self.assertNotIn(self.other_office, offered)

    def test_platform_kind_not_offered(self):
        self.add(kind="platform")
        self.assertFalse(User.objects.filter(username="petrova").exists())


class OwnCardTests(CompanyAdminCase):
    def test_own_roles_place_and_block_read_only(self):
        page = self.client.get(card(self.admin)).content.decode()
        self.assertNotIn('name="is_active"', page)
        self.assertNotIn('name="kind"', page)
        self.assertNotIn('name="assignments-0-roles"', page)
        self.assertIn('name="last_name"', page)  # личные данные - можно
        self.assertNotIn("/transfer/", page)
        self.assertNotIn("/dismiss/", page)

    def test_own_place_actions_refused(self):
        for action in ("transfer", "dismiss", "hire"):
            with refusal_expected(self):
                self.assertEqual(self.client.get(f"{USERS}{self.admin.pk}/{action}/").status_code, 403)

    def test_own_password_is_changed_not_reset(self):
        page = self.client.get(f"{USERS}{self.admin.pk}/reset-password/")
        self.assertRedirects(page, "/admin/password_change/", fetch_redirect_response=False)


class PersonnelActionTests(CompanyAdminCase):
    def test_buttons_on_colleague_card(self):
        page = self.client.get(card(self.ivanov)).content.decode()
        for label in ("Перевести", "Уволить", "Сбросить пароль"):
            self.assertIn(label, page)
        self.assertNotIn(">Принять<", page)

    def test_place_fields_read_only_roles_editable(self):
        page = self.client.get(card(self.ivanov)).content.decode()
        self.assertNotIn('name="assignments-0-department"', page)
        self.assertNotIn('name="assignments-0-valid_from"', page)
        self.assertIn('name="assignments-0-roles"', page)

    def test_transfer(self):
        old = self.place(self.ivanov)
        day = today()
        page = self.client.post(f"{USERS}{self.ivanov.pk}/transfer/", {
            "valid_from": day.strftime("%d.%m.%Y"), "department": self.accounting.pk,
            "position": self.accountant.pk, "roles": [self.references_role.pk], "visibility": "company",
            "basis": "Приказ 16-к",
        })
        self.assertRedirects(page, card(self.ivanov), fetch_redirect_response=False)
        old.refresh_from_db()
        self.assertEqual(old.valid_to, day - DAY)
        new = self.place(self.ivanov)
        self.assertEqual((new.department, new.valid_from, new.valid_to), (self.accounting, day, None))
        self.assertEqual(new.updated_by, self.admin)
        fresh = User.objects.get(pk=self.ivanov.pk)
        self.assertTrue(fresh.has_perm("counterparties.add_counterparty"))  # права - новые роли

    def test_transfer_not_before_current_start(self):
        page = self.client.post(f"{USERS}{self.ivanov.pk}/transfer/", {
            "valid_from": LONG_AGO.strftime("%d.%m.%Y"), "department": self.accounting.pk,
            "position": self.accountant.pk, "visibility": "company",
        })
        self.assertContains(page, "не раньше следующего дня")
        self.assertIsNone(self.place(self.ivanov).valid_to)

    def test_dismiss_today_blocks(self):
        page = self.client.post(f"{USERS}{self.ivanov.pk}/dismiss/", {"last_day": today().strftime("%d.%m.%Y")})
        self.assertRedirects(page, card(self.ivanov), fetch_redirect_response=False)
        fresh = User.objects.get(pk=self.ivanov.pk)
        self.assertFalse(fresh.is_active)
        self.assertEqual(fresh.updated_by, self.admin)
        self.assertEqual(Assignment.objects.get(user=fresh).valid_to, today())

    def test_dismiss_future_keeps_account_until_then(self):
        last_day = today() + 10 * DAY
        self.client.post(f"{USERS}{self.ivanov.pk}/dismiss/", {"last_day": last_day.strftime("%d.%m.%Y")})
        fresh = User.objects.get(pk=self.ivanov.pk)
        self.assertTrue(fresh.is_active)
        self.assertEqual(self.place(fresh).valid_to, last_day)

    def test_hire_after_dismissal_unblocks(self):
        personnel.dismiss(self.ivanov, last_day=today() - DAY, by=self.admin)
        self.assertIn(">Принять<", self.client.get(card(self.ivanov)).content.decode())
        page = self.client.post(f"{USERS}{self.ivanov.pk}/hire/", {
            "valid_from": today().strftime("%d.%m.%Y"), "department": self.head_office.pk,
            "position": self.analyst.pk, "roles": [self.employee_role.pk], "visibility": "company",
        })
        self.assertRedirects(page, card(self.ivanov), fetch_redirect_response=False)
        fresh = User.objects.get(pk=self.ivanov.pk)
        self.assertTrue(fresh.is_active)
        self.assertEqual(self.place(fresh).valid_from, today())

    def test_hire_refused_while_working(self):
        page = self.client.post(f"{USERS}{self.ivanov.pk}/hire/", {
            "valid_from": today().strftime("%d.%m.%Y"), "department": self.head_office.pk,
            "position": self.analyst.pk, "visibility": "company",
        })
        self.assertContains(page, "уже есть место")

    def test_roles_changed_in_block(self):
        page = self.post_card(self.ivanov, **{"assignments-0-roles": [str(self.references_role.pk)]})
        self.assertEqual(page.status_code, 302, page.content.decode()[:3000] if page.status_code == 200 else "")
        self.assertEqual(list(self.place(self.ivanov).roles.all()), [self.references_role])


class PasswordTests(CompanyAdminCase):
    def test_reset_shows_once_and_logs_who(self):
        self.assertContains(self.client.get(f"{USERS}{self.ivanov.pk}/reset-password/"), "Выдать временный пароль")
        page = self.client.post(f"{USERS}{self.ivanov.pk}/reset-password/")
        password = page.context["password"]
        self.assertContains(page, password)
        self.assertIn("no-store", page["Cache-Control"])
        fresh = User.objects.get(pk=self.ivanov.pk)
        self.assertTrue(fresh.check_password(password))
        self.assertTrue(fresh.must_change_password)
        entry = ChangeLog.objects.filter(field="password").latest("pk")
        self.assertEqual((entry.user, entry.new_value), (self.admin, "***"))
        self.assertFalse(ChangeLog.objects.filter(new_value__contains=password).exists())

    def test_old_password_link_goes_to_reset(self):
        page = self.client.get(f"{USERS}{self.ivanov.pk}/password/")
        self.assertRedirects(page, f"{USERS}{self.ivanov.pk}/reset-password/", fetch_redirect_response=False)

    def test_temporary_password_must_be_changed(self):
        password = personnel.issue_temporary_password(self.ivanov, by=self.admin)
        self.client.logout()
        self.assertTrue(self.client.login(username="ivanov", password=password))
        self.assertRedirects(self.client.get("/admin/"), "/admin/password_change/", fetch_redirect_response=False)
        new = "Lq8#vR3n!zWk5pT"
        page = self.client.post("/admin/password_change/", {
            "old_password": password, "new_password1": new, "new_password2": new,
        })
        self.assertRedirects(page, "/admin/password_change/done/", fetch_redirect_response=False)
        fresh = User.objects.get(pk=self.ivanov.pk)
        self.assertFalse(fresh.must_change_password)
        self.assertEqual(ChangeLog.objects.filter(field="password").latest("pk").user, fresh)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_generated_password_passes_rules(self):
        for _ in range(20):
            password = personnel.generate_password(self.ivanov)
            self.assertRegex(password, r"^[A-Za-z2-9]{4}-[A-Za-z2-9]{4}-[A-Za-z2-9]{4}$")


class LoginTests(CompanyAdminCase):
    def test_without_current_place_no_login(self):
        self.assertIsNotNone(authenticate(username="ivanov", password=STRONG_PASSWORD))
        personnel.dismiss(self.ivanov, last_day=today() + 3 * DAY, by=self.admin)
        self.assertIsNotNone(authenticate(username="ivanov", password=STRONG_PASSWORD))  # ещё работает
        place = self.place(self.ivanov)
        place.valid_to = today() - DAY
        place.save()
        self.assertIsNone(authenticate(username="ivanov", password=STRONG_PASSWORD))

    def test_future_start_no_login_yet(self):
        newcomer = self.make("newcomer")
        personnel.hire(newcomer, valid_from=today() + DAY, roles=[], by=self.admin,
                       place={"department": self.head_office, "position": self.analyst})
        self.assertIsNone(authenticate(username="newcomer", password=STRONG_PASSWORD))

    def test_platform_and_superuser_without_place(self):
        User.objects.create_user("platform", password=STRONG_PASSWORD, kind="platform")
        self.assertIsNotNone(authenticate(username="platform", password=STRONG_PASSWORD))
        self.assertIsNotNone(authenticate(username="mosaic_root", password=STRONG_PASSWORD))


class LastAdminTests(CompanyAdminCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second = cls.make("second", last_name="Второй")
        give_roles(cls.second, ADMIN)

    def end_admin_place(self, user, last_day):
        place = self.place(user)
        place.valid_to = last_day
        place.save()

    def test_two_admins_one_may_go(self):
        personnel.dismiss(self.second, last_day=today(), by=self.admin)
        self.assertFalse(User.objects.get(pk=self.second.pk).is_active)

    def test_dismiss_creates_future_gap(self):
        # «admin» уходит через 5 дней; уволить сегодня второго - через 6 дней никого.
        self.end_admin_place(self.admin, today() + 5 * DAY)
        page = self.client.post(f"{USERS}{self.second.pk}/dismiss/", {"last_day": today().strftime("%d.%m.%Y")})
        gap = today() + 6 * DAY
        self.assertContains(page, f"не останется администратора с {gap:%d.%m.%Y}")
        self.assertTrue(User.objects.get(pk=self.second.pk).is_active)
        self.assertIsNone(self.place(self.second).valid_to)

    def test_roles_in_block_keep_an_admin(self):
        self.end_admin_place(self.admin, today() + 5 * DAY)
        page = self.post_card(self.second, **{"assignments-0-roles": [str(self.employee_role.pk)]})
        self.assertContains(page, "не останется администратора")
        self.assertIn(self.admin_role, self.place(self.second).roles.all())

    def test_block_sole_admin_refused_even_for_superuser(self):
        self.end_admin_place(self.second, today() - DAY)
        root = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        self.client.force_login(root)
        page = self.post_card(self.admin, is_active="")
        self.assertContains(page, "не останется администратора")
        self.assertTrue(User.objects.get(pk=self.admin.pk).is_active)

    def test_company_without_admin_not_blocked(self):
        # В другой страховой администратора нет - правило не мешает её делам.
        give_roles(self.stranger, "Сотрудник")
        personnel.dismiss(self.stranger, last_day=today(), by=self.admin)
        self.assertFalse(User.objects.get(pk=self.stranger.pk).is_active)

    def test_transfer_without_admin_role_refused_for_sole(self):
        self.end_admin_place(self.admin, today() + 2 * DAY)
        page = self.client.post(f"{USERS}{self.second.pk}/transfer/", {
            "valid_from": (today() + DAY).strftime("%d.%m.%Y"), "department": self.accounting.pk,
            "position": self.accountant.pk, "roles": [self.employee_role.pk], "visibility": "company",
        })
        self.assertContains(page, "не останется администратора")


class RoleRightsTests(CompanyAdminCase):
    def test_company_admin_role_rights(self):
        group = self.admin_role.group
        codes = set(group.permissions.values_list("codename", flat=True))
        self.assertTrue({"view_user", "add_user", "change_user", "change_assignment", "add_jobposition",
                         "view_role", "add_department"} <= codes)
        self.assertFalse({"delete_user", "change_jobposition", "change_role"} & codes)
        self.assertIn("Свои роли", self.admin_role.description)

    def test_screen_text_has_no_select_placeholder(self):
        page = self.client.get(f"{USERS}add/").content.decode()
        self.assertIsNone(re.search(r"Select an option", page))
