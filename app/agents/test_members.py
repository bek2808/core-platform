"""
Автотесты пользователей агента (часть B3; модель: docs/users-model.md,
«Пользователь агента»).

Главное, что проверяется:
- пользователь агента заводится из карточки агента: учётная запись вида
  «агент» своей страховой, временный пароль от системы, связь с агентом;
- вход: только пока есть действующая связь с агентом и у агента -
  действующее соглашение; соглашение закончилось - вход закрыт сам;
  выключенная страховая - тоже закрыт;
- права - роль «Директор агента» или «Агент» по отметке «директор»;
- «Отключить»: последний день сегодня или прошёл - блокировка сразу;
  прекращение соглашения задним числом блокирует пользователей агента;
- вид «агент» не заводится на обычном экране «Пользователи», у пользователя
  агента нет прикреплений и кадровых кнопок;
- правила связи держит и база (триггер).

Запуск на сервере: bin/test agents
"""
import datetime

from django.contrib.auth import authenticate
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.backends import AssignmentBackend
from accounts.models import Assignment, JobPosition, User
from audit.models import ChangeLog
from config.testing import refusal_expected

from . import services
from .models import Member, agent_user_may_work
from .test_agents import STRONG_PASSWORD, AgentCase

D = datetime.date
AGENTS_URL = "/admin/agents/agent/"
USERS_URL = "/admin/accounts/user/"


class MemberCase(AgentCase):
    def setUp(self):
        super().setUp()
        self.inre_agent = self.agent()
        self.agreement_inre = self.agreement(self.inre_agent)  # бессрочное с 14.04.2021

    def add(self, username="inre_ali", **fields):
        fields = {"last_name": "Алиев", "first_name": "Али", "valid_from": D(2024, 1, 1), **fields}
        return services.add_member(self.inre_agent, username=username, by=self.director, **fields)


class AddMemberTests(MemberCase):
    def test_creates_agent_user_with_temporary_password(self):
        member, password = self.add(phone="+998 (90) 123-45-67", is_director=True)
        user = member.user
        self.assertEqual((user.kind, user.insurance_company, user.is_staff), (User.Kind.AGENT, self.mosaic, False))
        self.assertTrue(user.must_change_password)
        self.assertTrue(user.check_password(password))
        self.assertEqual(user.phone, "998901234567")
        self.assertEqual((member.agent, member.is_director, member.created_by), (self.inre_agent, True, self.director))
        self.assertTrue(ChangeLog.objects.filter(object_id=member.pk, field="is_director", user=self.director).exists())

    def test_username_taken(self):
        self.add()
        with self.assertRaises(ValidationError) as caught:
            self.add()
        self.assertIn("username", caught.exception.message_dict)
        self.assertEqual(User.objects.filter(username="inre_ali").count(), 1)


class LoginRuleTests(MemberCase):
    def test_may_work_while_agreement_and_membership_current(self):
        member, password = self.add()
        user = member.user
        self.assertTrue(agent_user_may_work(user))
        self.assertTrue(AssignmentBackend().user_can_authenticate(user))
        self.assertEqual(authenticate(username="inre_ali", password=password), user)
        self.assertFalse(agent_user_may_work(user, D(2023, 12, 31)))  # связь ещё не началась

    def test_agreement_ended_closes_login(self):
        member, password = self.add()
        self.agreement_inre.valid_to = D(2025, 12, 31)
        self.agreement_inre.save()
        self.assertFalse(agent_user_may_work(member.user))
        self.assertIsNone(authenticate(username="inre_ali", password=password))

    def test_company_switched_off_closes_login(self):
        member, password = self.add()
        self.mosaic.is_active = False
        self.mosaic.save()
        self.assertFalse(AssignmentBackend().user_can_authenticate(User.objects.get(pk=member.user_id)))
        self.mosaic.is_active = True
        self.mosaic.save()

    def test_rights_from_director_flag(self):
        # Права в роли агента придут с договорами; для проверки дадим роли одно право.
        right = Permission.objects.get(codename="view_counterparty")
        Group.objects.get(name="Директор агента").permissions.add(right)
        director, _ = self.add(username="inre_boss", is_director=True)
        seller, _ = self.add(username="seller")
        self.assertTrue(User.objects.get(pk=director.user_id).has_perm("counterparties.view_counterparty"))
        self.assertFalse(User.objects.get(pk=seller.user_id).has_perm("counterparties.view_counterparty"))
        self.agreement_inre.valid_to = D(2025, 12, 31)  # соглашение кончилось - прав нет
        self.agreement_inre.save()
        self.assertFalse(User.objects.get(pk=director.user_id).has_perm("counterparties.view_counterparty"))


class DisableTests(MemberCase):
    def test_disable_today_blocks_at_once(self):
        member, _ = self.add()
        services.disable_member(member, last_day=timezone.localdate(), by=self.director)
        member.user.refresh_from_db()
        self.assertFalse(member.user.is_active)
        self.assertEqual(member.valid_to, timezone.localdate())

    def test_disable_in_future_closes_login_after_last_day(self):
        member, _ = self.add()
        last_day = timezone.localdate() + datetime.timedelta(days=10)
        services.disable_member(member, last_day=last_day, by=self.director)
        member.user.refresh_from_db()
        self.assertTrue(member.user.is_active)
        self.assertTrue(agent_user_may_work(member.user, last_day))
        self.assertFalse(agent_user_may_work(member.user, last_day + datetime.timedelta(days=1)))

    def test_last_day_not_before_start(self):
        member, _ = self.add()
        with self.assertRaises(ValidationError):
            services.disable_member(member, last_day=D(2023, 1, 1), by=self.director)

    def test_terminating_agreement_in_past_blocks_members(self):
        member, _ = self.add()
        services.terminate(self.agreement_inre, last_day=timezone.localdate(), reason="Конец", by=self.director)
        member.user.refresh_from_db()
        self.assertFalse(member.user.is_active)


class MemberRulesTests(MemberCase):
    def test_only_agent_kind_user(self):
        member = Member(user=self.director, agent=self.inre_agent, valid_from=D(2024, 1, 1))
        self.assertIn("вида «агент»", self.errors(member)["user"][0])
        self.assert_refused_by_database(lambda: member.save())

    def test_other_company_user_refused_by_database(self):
        stranger = User(username="kz_agent", kind=User.Kind.AGENT, insurance_company=self.other)
        stranger.save()
        member = Member(user=stranger, agent=self.inre_agent, valid_from=D(2024, 1, 1))
        self.assertIn("другой страховой", self.errors(member)["user"][0])
        self.assert_refused_by_database(lambda: member.save())

    def test_periods_do_not_overlap(self):
        member, _ = self.add()
        second_agent = self.agent(self.moi)
        again = Member(user=member.user, agent=second_agent, valid_from=D(2025, 1, 1))
        self.assertIn("уже связан", self.errors(again)["valid_from"][0])
        self.assert_refused_by_database(lambda: again.save())

    def test_user_and_agent_fixed(self):
        member, _ = self.add()
        second_agent = self.agent(self.moi)
        self.assert_refused_by_database(
            lambda: Member._base_manager.filter(pk=member.pk).update(agent=second_agent)
        )

    def test_no_assignment_for_agent_user(self):
        member, _ = self.add()
        assignment = Assignment(user=member.user, department=self.head,
                                position=JobPosition.objects.get(name="Не указана"), valid_from=D(2024, 1, 1))
        self.assertIn("прикреплений нет", self.errors(assignment)["department"][0])


class MemberScreenTests(MemberCase):
    def login(self, *groups, username="admin_mosaic", company="mosaic"):
        from accounts.testing import give_roles

        user = User.objects.create_user(
            username, password=STRONG_PASSWORD, insurance_company=self.mosaic if company == "mosaic" else None,
        )
        give_roles(user, *groups)
        self.client.force_login(user)
        return user

    def card(self):
        return f"{AGENTS_URL}{self.inre_agent.pk}/change/"

    def test_add_member_shows_password_once(self):
        admin_user = self.login("Работа с агентами")
        self.assertContains(self.client.get(self.card()), "add-member/")
        page = self.client.post(f"{AGENTS_URL}{self.inre_agent.pk}/add-member/", {
            "username": "inre_ali", "last_name": "Алиев", "first_name": "Али", "valid_from": "2024-01-01",
            "is_director": "on", "phone": "+998 90 123 45 67",
        })
        self.assertEqual(page.status_code, 200)
        self.assertIn("no-store", page["Cache-Control"])  # пароль не оседает в кэше браузера
        password = page.context["password"]
        user = User.objects.get(username="inre_ali")
        self.assertTrue(user.check_password(password))
        self.assertContains(page, password)
        member = user.agent_memberships.get()
        self.assertEqual((member.is_director, member.created_by), (True, admin_user))
        card = self.client.get(self.card())
        self.assertContains(card, "Алиев Али")
        self.assertNotContains(card, password)  # пароль - только на одной странице

    def test_add_member_errors_shown(self):
        self.login("Администратор страховой")
        self.add(username="taken")
        page = self.client.post(f"{AGENTS_URL}{self.inre_agent.pk}/add-member/", {
            "username": "taken", "last_name": "Х", "first_name": "Y", "valid_from": "2024-01-01", "phone": "12",
        })
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "errornote")
        self.assertEqual(User.objects.filter(username="taken").count(), 1)

    def test_disable_and_reset_password(self):
        self.login("Работа с агентами")
        member, _ = self.add()
        page = self.client.post(f"{AGENTS_URL}{self.inre_agent.pk}/members/{member.pk}/password/")
        new_password = page.context["password"]
        member.user.refresh_from_db()
        self.assertTrue(member.user.check_password(new_password))
        page = self.client.post(f"{AGENTS_URL}{self.inre_agent.pk}/members/{member.pk}/disable/", {
            "last_day": timezone.localdate().isoformat(),
        })
        self.assertRedirects(page, self.card(), fetch_redirect_response=False)
        member.user.refresh_from_db()
        self.assertFalse(member.user.is_active)

    def test_member_of_other_agent_not_reachable(self):
        self.login("Администратор страховой")
        other_agent = self.agent(self.moi)
        member, _ = services.add_member(other_agent, username="moi_user", last_name="М", first_name="М",
                                        valid_from=D(2024, 1, 1), by=self.director)
        page = self.client.get(f"{AGENTS_URL}{self.inre_agent.pk}/members/{member.pk}/disable/")
        self.assertEqual(page.status_code, 404)

    def test_employee_and_platform_refused(self):
        member, _ = self.add()
        self.login("Сотрудник", username="clerk")
        with refusal_expected(self):
            self.assertEqual(self.client.get(f"{AGENTS_URL}{self.inre_agent.pk}/add-member/").status_code, 403)
        self.login("Администратор платформы", username="platform", company=None)
        with refusal_expected(self):
            self.assertEqual(
                self.client.post(f"{AGENTS_URL}{self.inre_agent.pk}/members/{member.pk}/password/").status_code, 403
            )

    def test_users_screen_has_no_agent_kind(self):
        self.login("Администратор страховой")
        page = self.client.get(f"{USERS_URL}add/").content.decode()
        kinds = page[page.index('name="kind"'):]
        kinds = kinds[:kinds.index("</select>")]
        self.assertNotIn('value="agent"', kinds)
        page = self.client.post(f"{USERS_URL}add/", {
            "username": "fake_agent", "last_name": "Х", "first_name": "Y", "kind": "agent",
            "department": str(self.head.pk), "position": str(JobPosition.objects.get(name="Не указана").pk),
            "valid_from": "2024-01-01",
        })
        self.assertEqual(page.status_code, 200)
        self.assertFalse(User.objects.filter(username="fake_agent").exists())

    def test_agent_user_card_without_personnel_actions(self):
        member, _ = self.add()
        self.login("Администратор страховой")
        page = self.client.get(f"{USERS_URL}{member.user_id}/change/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "/transfer/")
        self.assertNotContains(page, "/hire/")
        self.assertNotContains(page, "assignments-group")
        self.assertContains(page, "/reset-password/")  # пароль сбросить можно и здесь
