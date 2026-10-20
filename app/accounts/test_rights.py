"""
Автотесты прав по прикреплению (часть A2.3, accounts/backends.py).

Главное, что проверяется:
- права сотрудника - роли прикрепления, действующего сегодня: до начала
  и после закрытия прав нет, перевод сразу меняет права;
- группы в учётной записи действуют только у администратора платформы;
- заблокированный - без прав; суперпользователь - все права без ролей;
- «статус персонала» ставит система: у всех, кроме агентов; на экране его не поменять;
- миграция accounts/0006 переносит права из групп в прикрепления так,
  что никто не теряет прав, и откатывается обратно.

Запуск на сервере: bin/test accounts
"""
import datetime
import importlib

from django.apps import apps
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone

from companies.models import Department, InsuranceCompany

from .models import Assignment, JobPosition, Role, User
from .testing import give_roles

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
VIEW_COUNTERPARTY = "counterparties.view_counterparty"
ADD_COUNTERPARTY = "counterparties.add_counterparty"
VIEW_EXCHANGE = "integrations.view_exchange"
CHANGE_CONNECTION = "integrations.change_erspconnection"

migration = importlib.import_module("accounts.migrations.0006_rights_from_assignments")


def fresh(user):
    """Права кешируются в объекте пользователя - для проверки берём его заново."""
    return User.objects.get(pk=user.pk)


class RightsCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mosaic = InsuranceCompany.objects.create(name="Mosaic", code="mosaic", country="KZ")
        cls.head_office = Department.head_office_for(cls.mosaic)
        cls.position = JobPosition.objects.get(name="Бизнес-аналитик")
        cls.employee = Role.objects.get(code="employee")
        cls.references_admin = Role.objects.get(code="references_admin")
        cls.integrations_admin = Role.objects.get(code="integrations_admin")

    def make_user(self, username="ivanov", **fields):
        fields.setdefault("insurance_company", self.mosaic)
        return User.objects.create_user(username, password=STRONG_PASSWORD, **fields)

    def assign(self, user, roles, valid_from, valid_to=None):
        assignment = Assignment(user=user, department=self.head_office, position=self.position,
                                valid_from=valid_from, valid_to=valid_to)
        assignment.save()
        assignment.set_roles(roles)
        return assignment

    @property
    def today(self):
        return timezone.localdate()


class AssignmentRightsTests(RightsCase):
    def test_roles_of_current_assignment(self):
        user = self.make_user()
        self.assign(user, [self.references_admin], self.today - datetime.timedelta(days=30))
        user = fresh(user)
        self.assertTrue(user.has_perm(ADD_COUNTERPARTY))
        self.assertTrue(user.has_perm(VIEW_EXCHANGE))
        self.assertFalse(user.has_perm(CHANGE_CONNECTION))  # это роль интеграций

    def test_no_assignment_no_rights(self):
        self.assertEqual(fresh(self.make_user()).get_all_permissions(), set())

    def test_closed_assignment_gives_nothing(self):
        user = self.make_user()
        yesterday = self.today - datetime.timedelta(days=1)
        self.assign(user, [self.references_admin], yesterday - datetime.timedelta(days=30), yesterday)
        self.assertFalse(fresh(user).has_perm(VIEW_COUNTERPARTY))

    def test_future_assignment_gives_nothing_yet(self):
        user = self.make_user()
        self.assign(user, [self.references_admin], self.today + datetime.timedelta(days=1))
        self.assertFalse(fresh(user).has_perm(VIEW_COUNTERPARTY))

    def test_last_day_still_counts(self):
        user = self.make_user()
        self.assign(user, [self.employee], self.today - datetime.timedelta(days=10), self.today)
        self.assertTrue(fresh(user).has_perm(VIEW_COUNTERPARTY))  # «по» включительно

    def test_transfer_changes_rights_at_once(self):
        user = self.make_user()
        old = self.assign(user, [self.references_admin], self.today - datetime.timedelta(days=30))
        old.valid_to = self.today - datetime.timedelta(days=1)
        old.save()
        self.assign(user, [self.integrations_admin], self.today)
        user = fresh(user)
        self.assertTrue(user.has_perm(CHANGE_CONNECTION))
        self.assertFalse(user.has_perm(ADD_COUNTERPARTY))

    def test_account_groups_ignored_for_company_user(self):
        user = self.make_user()
        user.groups.add(Group.objects.get(name="Администратор справочников"))
        self.assertFalse(fresh(user).has_perm(ADD_COUNTERPARTY))

    def test_platform_uses_account_groups(self):
        platform = self.make_user("platform", insurance_company=None, kind=User.Kind.PLATFORM)
        platform.groups.add(Group.objects.get(name="Сотрудник"))
        self.assertTrue(fresh(platform).has_perm(VIEW_COUNTERPARTY))

    def test_blocked_user_has_no_rights(self):
        user = self.make_user()
        self.assign(user, [self.references_admin], self.today - datetime.timedelta(days=30))
        user.is_active = False
        user.save()
        self.assertEqual(fresh(user).get_all_permissions(), set())

    def test_superuser_has_everything_without_roles(self):
        root = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        self.assertTrue(fresh(root).has_perm(CHANGE_CONNECTION))

    def test_screen_follows_roles(self):
        user = self.make_user()
        self.client.force_login(user)
        self.assertNotContains(self.client.get("/admin/"), "/admin/counterparties/counterparty/")
        give_roles(user, "Администратор справочников")
        self.assertContains(self.client.get("/admin/"), "/admin/counterparties/counterparty/")


class StaffStatusTests(RightsCase):
    def test_set_by_system(self):
        self.assertTrue(self.make_user("employee", is_staff=False).is_staff)
        self.assertTrue(self.make_user("contractor", kind=User.Kind.CONTRACTOR).is_staff)
        self.assertFalse(self.make_user("agent", kind=User.Kind.AGENT, is_staff=True).is_staff)

    def test_agent_cannot_log_into_admin(self):
        self.make_user("agent", kind=User.Kind.AGENT)
        page = self.client.post("/admin/login/?next=/admin/", {"username": "agent", "password": STRONG_PASSWORD})
        self.assertEqual(page.status_code, 200)  # снова форма входа
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_not_editable_on_screen(self):
        root = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        user = self.make_user()
        self.client.force_login(root)
        page = self.client.get(f"/admin/accounts/user/{user.pk}/change/").content.decode()
        self.assertNotIn('name="is_staff"', page)
        self.assertNotIn('name="groups"', page)  # у сотрудника страховой группы не действуют

    def test_platform_card_shows_groups(self):
        root = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        platform = self.make_user("platform", insurance_company=None, kind=User.Kind.PLATFORM)
        self.client.force_login(root)
        self.assertContains(self.client.get(f"/admin/accounts/user/{platform.pk}/change/"), 'name="groups"')


class MigrationTests(RightsCase):
    """Перенос прав из групп в прикрепления (accounts/0006) - на настоящих моделях."""

    def test_user_without_assignment_gets_one_with_group_roles(self):
        user = self.make_user()
        user.groups.add(Group.objects.get(name="Администратор справочников"), Group.objects.get(name="Сотрудник"))
        migration.move_rights(apps, None)
        assignment = Assignment.objects.get(user=user)
        self.assertEqual((assignment.department, assignment.position.name), (self.head_office, "Не указана"))
        self.assertEqual(assignment.basis, migration.MIGRATION_BASIS)
        self.assertEqual(set(assignment.roles.values_list("code", flat=True)), {"references_admin", "employee"})
        self.assertFalse(user.groups.exists())
        self.assertTrue(fresh(user).has_perm(ADD_COUNTERPARTY))  # права не потеряны

    def test_existing_assignment_kept_and_groups_added(self):
        user = self.make_user()
        own = self.assign(user, [self.integrations_admin], self.today - datetime.timedelta(days=30))
        user.groups.add(Group.objects.get(name="Сотрудник"))
        migration.move_rights(apps, None)
        self.assertEqual(list(Assignment.objects.filter(user=user)), [own])  # второе не заведено
        self.assertEqual(set(own.roles.values_list("code", flat=True)), {"integrations_admin", "employee"})

    def test_platform_groups_untouched(self):
        platform = self.make_user("platform", insurance_company=None, kind=User.Kind.PLATFORM)
        platform.groups.add(Group.objects.get(name="Сотрудник"))
        migration.move_rights(apps, None)
        self.assertTrue(platform.groups.exists())
        self.assertFalse(Assignment.objects.filter(user=platform).exists())

    def test_rollback_returns_groups(self):
        user = self.make_user()
        user.groups.add(Group.objects.get(name="Администратор справочников"))
        migration.move_rights(apps, None)
        migration.return_groups(apps, None)
        self.assertFalse(Assignment.objects.filter(user=user).exists())
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["Администратор справочников"])

    def test_repeat_is_harmless(self):
        user = self.make_user()
        user.groups.add(Group.objects.get(name="Сотрудник"))
        migration.move_rights(apps, None)
        migration.move_rights(apps, None)
        self.assertEqual(Assignment.objects.filter(user=user).count(), 1)
