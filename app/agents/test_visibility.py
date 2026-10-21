"""
Автотесты видимости агентов (часть B4; модель: docs/users-model.md,
«Видимость данных» и «Кто работает с агентами», решение 25).

Главное, что проверяется:
- «вся страховая» - все агенты своей страховой; «своё подразделение»
  в головном офисе - тоже все (решение 25); в отделе - только агенты
  этого отдела; руководитель филиала - филиал и его отделы, остальные
  сотрудники филиала - только сам филиал; «свои» - никого (куратора нет);
- агент без места на сегодня (место закончилось) - только «вся страховая»;
  будущее место видно сразу (иначе прикрепивший получил бы «не найдено»);
- перевод: до даты агента видят оба подразделения, после - только новое;
- экран: в списке - только видимые; чужая карточка и кнопки не открываются;
  прикрепить и перевести - только в видимые подразделения;
- журнал изменений не показывает невидимых агентов (и в «Истории»);
- контрагенты по-прежнему видны всей страховой (решение 3).

Запуск на сервере: bin/test agents
"""
from django.utils import timezone

from accounts.models import User, Visibility
from accounts.testing import give_roles
from accounts.visibility import Kind, scope_for, visible
from audit.models import ChangeLog
from config.testing import refusal_expected
from counterparties.models import Counterparty

from . import services
from .models import Agent, Member, Placement, visible_agents
from .test_agents import AGENTS_URL, D, JIZZAKH, STRONG_PASSWORD, URBAN, AgentCase, legal_entity

AGENTS_ROLE = "Работа с агентами"


class VisibilityCase(AgentCase):
    """
    Mosaic в тестах: головной офис (и отдел «Аналитика и ИТ» в нём), филиал
    Джизак с отделом продаж, филиал Самарканд. В каждом - свой агент.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.branch_sales = cls.department("division", "Отдел продаж", parent=cls.branch)
        cls.samarkand = cls.department("branch", "Филиал Самарканд", region_id=JIZZAKH)
        cls.today = timezone.localdate()

        cls.at_head = cls.placed(cls.inre, cls.head)
        cls.at_division = cls.placed(cls.moi, cls.division)
        cls.at_branch = cls.placed(cls.merolla, cls.branch)
        cls.at_branch_sales = cls.placed(legal_entity(cls.mosaic, "301000001", "Продажи Джизак"), cls.branch_sales)
        cls.at_samarkand = cls.placed(legal_entity(cls.mosaic, "301000002", "Самарканд Брокер"), cls.samarkand)
        # Место закончилось, новое не открыто.
        cls.gone = cls.placed(legal_entity(cls.mosaic, "301000003", "Бывший агент"), cls.branch,
                              valid_from=D(2020, 1, 1), valid_to=D(2021, 1, 1))
        # Прикреплён к филиалу с будущей даты.
        cls.future = cls.placed(legal_entity(cls.mosaic, "301000004", "Будущий агент"), cls.branch,
                                valid_from=D(2099, 1, 1))
        cls.foreign_agent = Agent(counterparty=cls.foreign, area_type_id=URBAN)
        cls.foreign_agent.save()

        cls.mosaic_agents = {cls.at_head, cls.at_division, cls.at_branch, cls.at_branch_sales,
                             cls.at_samarkand, cls.gone, cls.future}

    @classmethod
    def department(cls, kind, name, **fields):
        from companies.models import Department

        department = Department(insurance_company=cls.mosaic, kind=kind, name=name, **fields)
        department.save()
        return department

    @classmethod
    def placed(cls, counterparty, department, valid_from=D(2021, 1, 1), valid_to=None):
        agent = Agent(counterparty=counterparty, area_type_id=URBAN)
        agent.save()
        Placement(agent=agent, department=department, valid_from=valid_from, valid_to=valid_to).save()
        return agent

    @classmethod
    def employee(cls, username, department, visibility, is_head=False, roles=(AGENTS_ROLE,)):
        """Сотрудник Mosaic с местом в подразделении и видимостью."""
        user = User.objects.create_user(username, password=STRONG_PASSWORD, insurance_company=cls.mosaic,
                                        last_name=username.title())
        place = give_roles(user, department=department)
        place.visibility = visibility
        place.is_head = is_head
        place.save()
        if roles:
            give_roles(user, *roles)
        return user

    def seen(self, user, day=None):
        return set(visible_agents(scope_for(user, day)))


class ScopeTests(VisibilityCase):
    def test_whole_company(self):
        user = self.employee("all", self.branch, Visibility.COMPANY)
        self.assertEqual(self.seen(user), self.mosaic_agents)  # без агента чужой страховой

    def test_head_office_department_sees_everyone(self):
        user = self.employee("ho", self.head, Visibility.DEPARTMENT)
        self.assertEqual(scope_for(user).kind, Kind.COMPANY)
        self.assertEqual(self.seen(user), self.mosaic_agents)

    def test_division_of_head_office_sees_only_itself(self):
        user = self.employee("it", self.division, Visibility.DEPARTMENT, is_head=True)
        self.assertEqual(self.seen(user), {self.at_division})

    def test_branch_head_sees_branch_and_its_divisions(self):
        user = self.employee("boss", self.branch, Visibility.DEPARTMENT, is_head=True)
        self.assertEqual(self.seen(user), {self.at_branch, self.at_branch_sales, self.future})

    def test_branch_employee_sees_only_branch(self):
        user = self.employee("clerk", self.branch, Visibility.DEPARTMENT)
        self.assertEqual(self.seen(user), {self.at_branch, self.future})

    def test_branch_division_sees_only_itself(self):
        user = self.employee("sales", self.branch_sales, Visibility.DEPARTMENT, is_head=True)
        self.assertEqual(self.seen(user), {self.at_branch_sales})

    def test_own_sees_no_agents(self):
        user = self.employee("own", self.branch, Visibility.OWN, roles=("Сотрудник",))
        scope = scope_for(user)
        self.assertEqual(self.seen(user), set())
        self.assertFalse(scope.sees_departments)
        from companies.models import Department

        self.assertFalse(scope.departments(Department.objects.all()).exists())

    def test_agent_without_place_today_only_for_whole_company(self):
        for user in (self.employee("boss", self.branch, Visibility.DEPARTMENT, is_head=True),
                     self.employee("ho", self.head, Visibility.DEPARTMENT)):
            self.assertEqual(self.gone in self.seen(user), user.username == "ho")

    def test_transfer_seen_by_both_until_date_then_by_new(self):
        services.transfer(self.at_branch, department=self.samarkand, valid_from=D(2099, 1, 1),
                          by=self.director)
        jizzakh = self.employee("clerk", self.branch, Visibility.DEPARTMENT)
        samarkand = self.employee("clerk2", self.samarkand, Visibility.DEPARTMENT)
        self.assertIn(self.at_branch, self.seen(jizzakh))
        self.assertIn(self.at_branch, self.seen(samarkand))
        later = D(2099, 1, 1)
        self.assertNotIn(self.at_branch, self.seen(jizzakh, later))
        self.assertIn(self.at_branch, self.seen(samarkand, later))

    def test_nobody_else_sees_agents(self):
        platform = User.objects.create_user("platform", kind=User.Kind.PLATFORM)
        no_place = User.objects.create_user("no_place", insurance_company=self.mosaic)
        agent_user = User.objects.create_user("agent_user", insurance_company=self.mosaic, kind=User.Kind.AGENT)
        Member(user=agent_user, agent=self.at_branch, valid_from=D(2021, 1, 1)).save()
        for user in (platform, no_place, agent_user):
            self.assertEqual(scope_for(user).kind, Kind.NONE, user.username)
            self.assertEqual(self.seen(user), set(), user.username)

    def test_superuser(self):
        root = User.objects.create_superuser("root", password=STRONG_PASSWORD)
        self.assertEqual(self.seen(root), {*self.mosaic_agents, self.foreign_agent})

    def test_counterparties_stay_visible_to_whole_company(self):
        # Решение 3: клиентская база своей страховой видна всем её сотрудникам.
        user = self.employee("own", self.branch_sales, Visibility.OWN, roles=("Сотрудник",))
        self.assertEqual(set(visible(_Request(user), Counterparty.objects.all())),
                         set(Counterparty.objects.filter(insurance_company=self.mosaic)))


class _Request:
    def __init__(self, user):
        self.user = user


class ScreenTests(VisibilityCase):
    """Экран «Агенты» у сотрудника филиала с видимостью «своё подразделение»."""

    def setUp(self):
        self.boss = self.employee("boss", self.branch, Visibility.DEPARTMENT, is_head=True)
        self.client.force_login(self.boss)

    def change_url(self, agent):
        return f"{AGENTS_URL}{agent.pk}/change/"

    def options(self, page, name):
        page = page.content.decode()
        select = page[page.index(f'name="{name}"'):]
        return select[:select.index("</select>")]

    def test_list_shows_only_visible(self):
        page = self.client.get(AGENTS_URL)
        self.assertContains(page, "MEROLLA")          # филиал
        self.assertContains(page, "Продажи Джизак")   # отдел филиала
        self.assertNotContains(page, "InRe")          # головной офис
        self.assertNotContains(page, "Самарканд Брокер")

    def test_other_card_and_buttons_do_not_open(self):
        self.assertEqual(self.client.get(self.change_url(self.at_branch)).status_code, 200)
        # Невидимая карточка - как несуществующая: Django уводит в список с сообщением.
        self.assertEqual(self.client.get(self.change_url(self.at_head)).status_code, 302)
        for action in ("transfer", "place", "terminate", "add-member"):
            self.assertEqual(self.client.get(f"{AGENTS_URL}{self.at_head.pk}/{action}/").status_code, 404, action)
        self.assertEqual(self.client.get(f"{AGENTS_URL}{self.at_head.pk}/transfer/").status_code, 404)

    def test_departments_offered_only_visible(self):
        choices = self.options(self.client.get(f"{AGENTS_URL}add/"), "department")
        self.assertIn(f'value="{self.branch.pk}"', choices)
        self.assertIn(f'value="{self.branch_sales.pk}"', choices)
        self.assertNotIn(f'value="{self.head.pk}"', choices)
        self.assertNotIn(f'value="{self.samarkand.pk}"', choices)
        choices = self.options(self.client.get(f"{AGENTS_URL}{self.at_branch.pk}/transfer/"), "department")
        self.assertIn(f'value="{self.branch_sales.pk}"', choices)
        self.assertNotIn(f'value="{self.samarkand.pk}"', choices)

    def test_cannot_transfer_to_invisible_department(self):
        page = self.client.post(f"{AGENTS_URL}{self.at_branch.pk}/transfer/", {
            "department": str(self.samarkand.pk), "valid_from": "2099-01-01", "basis": "",
        })
        self.assertEqual(page.status_code, 200)  # ошибка в форме, а не перевод
        self.assertEqual(self.at_branch.placements.count(), 1)
        page = self.client.post(f"{AGENTS_URL}{self.at_branch.pk}/transfer/", {
            "department": str(self.branch_sales.pk), "valid_from": "2099-01-01", "basis": "",
        })
        self.assertEqual(page.status_code, 302)
        self.assertEqual(self.at_branch.placements.count(), 2)

    def test_new_agent_only_into_visible_department(self):
        free = legal_entity(self.mosaic, "301000005", "Новый брокер")
        data = {"counterparty": str(free.pk), "kind": "broker", "area_type": str(URBAN),
                "place_from": "2026-01-01", "place_basis": "", "formation_date": ""}
        page = self.client.post(f"{AGENTS_URL}add/", {**data, "department": str(self.head.pk)})
        self.assertEqual(page.status_code, 200)
        self.assertFalse(Agent.objects.filter(counterparty=free).exists())
        page = self.client.post(f"{AGENTS_URL}add/", {**data, "department": str(self.branch.pk)})
        agent = Agent.objects.get(counterparty=free)
        # Сразу в карточку - и она открывается: агент в его филиале.
        self.assertRedirects(page, self.change_url(agent))

    def test_own_visibility_cannot_add(self):
        # Роль «Работа с агентами» с «свои» сохранить нельзя (accounts), а без
        # видимых подразделений и кнопки «Добавить» нет.
        user = self.employee("own", self.branch, Visibility.OWN, roles=("Администратор страховой",))
        self.client.force_login(user)
        with refusal_expected(self):
            self.assertEqual(self.client.get(f"{AGENTS_URL}add/").status_code, 403)
        self.assertNotContains(self.client.get(AGENTS_URL), "MEROLLA")

    def journal_agents(self, response):
        """Номера агентов, чьи строки (сам агент или его записи) попали в список журнала."""
        seen = set()
        for row in response.context["cl"].queryset.select_related("content_type", "root_content_type"):
            if row.content_type.model_class() is Agent:
                seen.add(row.object_id)
            elif row.root_content_type is not None and row.root_content_type.model_class() is Agent:
                seen.add(row.root_object_id)
        return seen

    def test_journal_hides_invisible_agents(self):
        services.transfer(self.at_head, department=self.division, valid_from=D(2026, 1, 1), by=self.director)
        services.transfer(self.at_branch, department=self.branch_sales, valid_from=D(2026, 1, 1), by=self.director)
        self.assertTrue(ChangeLog.objects.filter(root_object_id=self.at_head.pk).exists())
        seen = self.journal_agents(self.client.get("/admin/audit/changelog/"))
        self.assertIn(self.at_branch.pk, seen)
        self.assertNotIn(self.at_head.pk, seen)
        # «История» чужого агента пуста, своего - нет.
        history = self.client.get(f"{AGENTS_URL}{self.at_head.pk}/history/", follow=True)
        self.assertEqual(self.journal_agents(history), set())
        history = self.client.get(f"{AGENTS_URL}{self.at_branch.pk}/history/", follow=True)
        self.assertEqual(self.journal_agents(history), {self.at_branch.pk})

    def test_journal_hides_users_of_invisible_agents(self):
        services.add_member(self.at_head, username="inre_user", last_name="Иванов", first_name="Иван",
                            by=self.director, valid_from=D(2026, 1, 1))
        services.add_member(self.at_branch, username="merolla_user", last_name="Петров", first_name="Пётр",
                            by=self.director, valid_from=D(2026, 1, 1))
        rows = self.client.get("/admin/audit/changelog/").context["cl"].queryset
        users = {row.object_repr for row in rows if row.content_type.model == "user"}
        self.assertTrue(any("merolla_user" in name or "Петров" in name for name in users), users)
        self.assertFalse(any("inre_user" in name or "Иванов" in name for name in users), users)

    def test_whole_company_unchanged(self):
        admin = self.employee("admin", self.branch, Visibility.COMPANY, roles=("Администратор страховой",))
        self.client.force_login(admin)
        page = self.client.get(AGENTS_URL)
        for name in ("InRe", "MEROLLA", "Самарканд Брокер", "Бывший агент"):
            self.assertContains(page, name)
        self.assertEqual(self.client.get(self.change_url(self.at_head)).status_code, 200)
