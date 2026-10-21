"""
Автотесты агентов (часть B2; модель: docs/users-model.md, «Агенты (этап B)»).

Главное, что проверяется:
- агент: только юрлицо своей страховой, один агент на контрагента,
  контрагент не меняется, дата образования - из свидетельства юрлица,
  правила «электронного» агента и счёта для комиссии;
- соглашение: подписант - пользователь, работавший на дату заключения,
  или ФИО текстом (решение 35); доверенность; периоды не пересекаются;
  номер не повторяется в страховой; досрочное прекращение;
- место агента: прикрепить, перевести (старое закрывается накануне),
  подразделение своей страховой; подразделение с агентами не закрыть;
- связь агента с ЕРСП и его agencyId;
- каждое правило между таблицами держит и база (триггер);
- экраны - от лица обычных сотрудников: администратор страховой
  и «Работа с агентами» работают, «Сотрудник» и платформа - нет;
  ошибки - сообщением, а не ошибкой 500 (в том числе пересечение
  соглашений, где одно поле только для чтения).

Запуск на сервере: bin/test agents
"""
import datetime

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from accounts.testing import give_roles
from audit.models import ChangeLog, UntrackedChangeError
from companies.models import Department, InsuranceCompany
from config.testing import refusal_expected
from counterparties.models import BankAccount, Counterparty, Individual, LegalEntity
from integrations.models import Environment, ErspAgency
from references.loader import load_from_files

from . import services
from .models import Agent, Agreement, ErspLink, Placement, agency_id_for_agent

STRONG_PASSWORD = "Kx7#mQ2v!pLr9zT"
TASHKENT, JIZZAKH, URBAN = 10, 13, 1
AGENTS_URL = "/admin/agents/agent/"
D = datetime.date


def legal_entity(company, inn, name, reg_date=None):
    """Контрагент-юрлицо, как его заводят в справочнике."""
    card = Counterparty(insurance_company=company)
    card.save()
    data = LegalEntity(counterparty=card, insurance_company=company, inn=inn, full_name=name,
                       short_name=name, reg_certificate_date=reg_date)
    data.save()
    card.refresh_from_legal_entity(data)
    return card


def individual(company, pinfl, last_name="FAMILIYA", first_name="ISM"):
    """Контрагент-физлицо с данными, как его заводит справочник."""
    card = Counterparty(insurance_company=company, kind=Counterparty.Kind.INDIVIDUAL)
    card.save()
    Individual(counterparty=card, last_name=last_name, first_name=first_name, pinfl=pinfl,
               birth_date=D(1990, 1, 1)).save()
    card.refresh_from_db()
    return card


class AgentCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_from_files()  # справочники ЕРСП - один раз на группу тестов
        cls.mosaic = InsuranceCompany.objects.create(
            name="Mosaic Insurance Group", code="mosaic", ersp_insurer_id=1084, ersp_region_id=TASHKENT
        )
        cls.other = InsuranceCompany.objects.create(name="Test Insurance KZ", code="test-kz", country="KZ")
        cls.head = Department.head_office_for(cls.mosaic)
        cls.other_head = Department.head_office_for(cls.other)
        cls.branch = Department(insurance_company=cls.mosaic, kind="branch", name="Филиал Джизак", region_id=JIZZAKH)
        cls.branch.save()
        cls.division = Department(insurance_company=cls.mosaic, kind="division", name="Аналитика и ИТ", parent=cls.head)
        cls.division.save()
        cls.inre = legal_entity(cls.mosaic, "201070533", 'ООО "InRe"', D(2015, 3, 1))
        cls.moi = legal_entity(cls.mosaic, "305861871", 'СП ООО "MOI Insurance Brokers Asia"')
        cls.merolla = legal_entity(cls.mosaic, "304935233", 'ЧП "MEROLLA"')
        cls.foreign = legal_entity(cls.other, "999999999", "Чужая организация")
        # Карточка физлица без данных (ФИО, ПИНФЛ) - агентом быть не может; с данными - может.
        cls.person = Counterparty(insurance_company=cls.mosaic, kind=Counterparty.Kind.INDIVIDUAL)
        cls.person.save()
        cls.person_with_data = individual(cls.mosaic, "30101990000000")
        # Генеральный директор: в системе с 2000 года (give_roles - прикрепление с 01.01.2000).
        cls.director = User.objects.create_user("director", insurance_company=cls.mosaic, last_name="Халилов")
        give_roles(cls.director)
        # Сотрудник, принятый в 2026: на дату старых соглашений он ещё не работал.
        cls.newcomer = User.objects.create_user("newcomer", insurance_company=cls.mosaic, last_name="Новиков")
        cls.newcomer_place = give_roles(cls.newcomer)
        cls.newcomer_place.valid_from = D(2026, 1, 1)
        cls.newcomer_place.save()
        cls.stranger = User.objects.create_user("stranger", insurance_company=cls.other, last_name="Чужой")
        give_roles(cls.stranger)

    def agent(self, counterparty=None, check=True, **fields):
        """Завести агента так же, как экран: сначала проверка, потом сохранение."""
        fields.setdefault("area_type_id", URBAN)
        agent = Agent(counterparty=counterparty or self.inre, **fields)
        if check:
            agent.full_clean()
        agent.save()
        return agent

    def agreement(self, agent, check=True, **fields):
        fields = {"number": "AZM/21-000", "signed_on": D(2021, 4, 14), "valid_from": D(2021, 4, 14),
                  "signer_name": "Халилов О.Н.", "signer_position": "Генеральный директор",
                  "signer_basis": Agreement.SignerBasis.CHARTER, **fields}
        agreement = Agreement(agent=agent, **fields)
        if check:
            agreement.full_clean()
        agreement.save()
        return agreement

    def errors(self, record):
        with self.assertRaises(ValidationError) as caught:
            record.full_clean()
        return caught.exception.message_dict

    def assert_refused_by_database(self, action):
        with self.assertRaises((IntegrityError, DatabaseError)), transaction.atomic():
            action()


class AgentRulesTests(AgentCase):
    def test_company_and_formation_date_from_counterparty(self):
        agent = self.agent()
        self.assertEqual(agent.insurance_company, self.mosaic)
        self.assertEqual(agent.formation_date, D(2015, 3, 1))  # дата свидетельства юрлица
        self.assertEqual(str(agent), 'ООО "InRe"')

    def test_individual_without_data_refused(self):
        errors = self.errors(Agent(counterparty=self.person, area_type_id=URBAN, formation_date=D(2026, 1, 1)))
        self.assertIn("нет данных", errors["counterparty"][0])

    def test_individual_agent(self):
        agent = self.agent(self.person_with_data, formation_date=D(2026, 1, 1))
        self.assertEqual(agent.insurance_company, self.mosaic)
        self.assertEqual(agent.formation_date, D(2026, 1, 1))
        self.assertEqual(str(agent), "FAMILIYA ISM")

    def test_individual_needs_formation_date_by_hand(self):
        errors = self.errors(Agent(counterparty=self.person_with_data, area_type_id=URBAN))
        self.assertIn("дату образования", errors["formation_date"][0])

    def test_individual_cannot_be_broker(self):
        errors = self.errors(Agent(counterparty=self.person_with_data, area_type_id=URBAN, kind="broker",
                                   formation_date=D(2026, 1, 1)))
        self.assertIn("только организация", errors["kind"][0])

    def test_individual_one_agent_per_counterparty(self):
        self.agent(self.person_with_data, formation_date=D(2026, 1, 1))
        errors = self.errors(Agent(counterparty=self.person_with_data, area_type_id=URBAN,
                                   formation_date=D(2026, 1, 1)))
        self.assertIn("уже заведён агентом", errors["counterparty"][0])

    def test_individual_of_other_company_refused(self):
        stranger = individual(self.other, "30101990000099")
        agent = Agent(counterparty=stranger, area_type_id=URBAN, insurance_company=self.mosaic,
                      formation_date=D(2026, 1, 1))
        self.assertIn("другой страховой", self.errors(agent)["counterparty"][0])

    def test_other_company_counterparty_refused(self):
        agent = Agent(counterparty=self.foreign, area_type_id=URBAN, insurance_company=self.mosaic)
        self.assertIn("другой страховой", self.errors(agent)["counterparty"][0])
        self.assert_refused_by_database(lambda: agent.save())

    def test_one_agent_per_counterparty(self):
        self.agent()
        self.assertIn("уже заведён агентом", self.errors(Agent(counterparty=self.inre, area_type_id=URBAN))["counterparty"][0])
        self.assert_refused_by_database(lambda: self.agent(check=False))

    def test_counterparty_fixed(self):
        agent = self.agent()
        agent.counterparty = self.moi
        self.assertIn("не меняется", self.errors(agent)["counterparty"][0])
        self.assert_refused_by_database(
            lambda: Agent._base_manager.filter(pk=agent.pk).update(counterparty=self.moi)
        )

    def test_electronic_rules(self):
        head = self.agent(is_electronic=True, is_head_electronic=True)
        child = self.agent(self.moi, is_electronic=True, head_agent=head)
        self.assertEqual(child.head_agent, head)
        # «Головной» без «электронного» система снимает сама; «входит в» - тоже.
        plain = self.agent(self.merolla, is_head_electronic=True, head_agent=head)
        self.assertEqual((plain.is_head_electronic, plain.head_agent), (False, None))

    def test_head_agent_must_be_head_electronic(self):
        not_head = self.agent(is_electronic=True)
        child = Agent(counterparty=self.moi, area_type_id=URBAN, is_electronic=True, head_agent=not_head)
        self.assertIn("не головной электронный", self.errors(child)["head_agent"][0])
        child.normalize()
        self.assert_refused_by_database(lambda: Agent._base_manager.bulk_create([child]))

    def test_commission_account_of_own_counterparty(self):
        own = BankAccount(counterparty=self.inre, foreign_bank_name="Bank", number="ABCDE12345", currency_id=860)
        own.save()
        alien = BankAccount(counterparty=self.moi, foreign_bank_name="Bank", number="FGHIJ67890", currency_id=860)
        alien.save()
        agent = self.agent(commission_account=own)
        agent.commission_account = alien
        self.assertIn("другого контрагента", self.errors(agent)["commission_account"][0])
        self.assert_refused_by_database(
            lambda: Agent._base_manager.filter(pk=agent.pk).update(commission_account=alien)
        )

    def test_no_delete(self):
        with self.assertRaises(UntrackedChangeError):
            self.agent().delete()


class AgreementRulesTests(AgentCase):
    def test_signer_as_text_for_old_agreement(self):
        agreement = self.agreement(self.agent())
        self.assertEqual(agreement.signer(), "Халилов О.Н., Генеральный директор")
        self.assertEqual(agreement.insurance_company, self.mosaic)

    def test_signer_required(self):
        errors = self.errors(Agreement(agent=self.agent(), number="1", signed_on=D(2021, 1, 1),
                                       valid_from=D(2021, 1, 1), signer_basis="charter"))
        self.assertIn("кто подписал", errors["signed_by"][0])

    def test_signer_user_working_on_date(self):
        agreement = self.agreement(self.agent(), signed_by=self.director, signer_name="")
        self.assertEqual(agreement.signer(), "Халилов")
        self.assertEqual(agreement.signer_name, "")  # ФИО - из учётной записи

    def test_signer_user_not_working_on_date(self):
        errors = self.errors(Agreement(agent=self.agent(), number="1", signed_on=D(2021, 4, 14),
                                       valid_from=D(2021, 4, 14), signed_by=self.newcomer, signer_basis="charter"))
        self.assertIn("не работал(а) в страховой 14.04.2021", errors["signed_by"][0])

    def test_signer_from_other_company_refused(self):
        agent = self.agent()
        agreement = Agreement(agent=agent, number="1", signed_on=D(2021, 4, 14), valid_from=D(2021, 4, 14),
                              signed_by=self.stranger, signer_basis="charter")
        self.assertIn("другой страховой", self.errors(agreement)["signed_by"][0])
        self.assert_refused_by_database(lambda: agreement.save())

    def test_power_of_attorney(self):
        agent = self.agent()
        errors = self.errors(Agreement(agent=agent, number="1", signed_on=D(2021, 1, 1), valid_from=D(2021, 1, 1),
                                       signer_name="Иванов", signer_basis="power_of_attorney"))
        self.assertIn("power_of_attorney_number", errors)
        self.assertIn("power_of_attorney_date", errors)
        agreement = self.agreement(agent, signer_basis="power_of_attorney", power_of_attorney_number=" 12 ",
                                   power_of_attorney_date=D(2021, 1, 10))
        self.assertEqual(agreement.power_of_attorney_number, "12")
        # Другое основание - номер и дата доверенности очищаются.
        agreement.signer_basis = "charter"
        agreement.full_clean()
        agreement.save()
        self.assertEqual((agreement.power_of_attorney_number, agreement.power_of_attorney_date), ("", None))

    def test_periods_do_not_overlap(self):
        agent = self.agent()
        self.agreement(agent, valid_to=D(2022, 4, 13))
        overlapping = Agreement(agent=agent, number="2", signed_on=D(2022, 1, 1), valid_from=D(2022, 1, 1),
                                signer_name="Халилов", signer_basis="charter")
        self.assertIn("уже есть соглашение", self.errors(overlapping)["valid_from"][0])
        self.assert_refused_by_database(lambda: overlapping.save())
        self.agreement(agent, number="2", signed_on=D(2022, 4, 14), valid_from=D(2022, 4, 14))  # продление

    def test_renewal_after_termination(self):
        agent = self.agent()
        first = self.agreement(agent)  # бессрочное
        services.terminate(first, last_day=D(2023, 12, 31), reason="Перезаключение", by=self.director)
        self.agreement(agent, number="2", signed_on=D(2024, 1, 1), valid_from=D(2024, 1, 1))
        self.assertEqual(agent.current_agreement().number, "2")

    def test_number_unique_in_company(self):
        self.agreement(self.agent())
        errors = self.errors(Agreement(agent=self.agent(self.moi), number="AZM/21-000", signed_on=D(2021, 1, 1),
                                       valid_from=D(2021, 1, 1), signer_name="Халилов", signer_basis="charter"))
        self.assertIn("number", errors)

    def test_dates_order(self):
        errors = self.errors(Agreement(agent=self.agent(), number="1", signed_on=D(2021, 1, 1),
                                       valid_from=D(2021, 5, 1), valid_to=D(2021, 4, 30),
                                       signer_name="Халилов", signer_basis="charter"))
        self.assertIn("valid_to", errors)

    def test_working_by_dates(self):
        agent = self.agent()
        self.agreement(agent, valid_to=D(2030, 12, 31))
        self.assertTrue(agent.is_working())
        self.assertFalse(agent.is_working(D(2021, 4, 13)))
        self.assertFalse(agent.is_working(D(2031, 1, 1)))

    def test_archived_counterparty_no_new_agreement(self):
        agent = self.agent()
        self.inre.status = Counterparty.Status.ARCHIVED
        self.inre.save()
        errors = self.errors(Agreement(agent=agent, number="1", signed_on=D(2021, 1, 1), valid_from=D(2021, 1, 1),
                                       signer_name="Халилов", signer_basis="charter"))
        self.assertIn("в архиве", errors["agent"][0])

    def test_agreement_company_mismatch_refused_by_database(self):
        agent = self.agent()

        def forged():
            agreement = Agreement(agent=agent, number="1", signed_on=D(2021, 1, 1), valid_from=D(2021, 1, 1),
                                  signer_name="Х", signer_basis="charter")
            agreement.normalize()
            agreement.insurance_company = self.other
            Agreement._base_manager.bulk_create([agreement])
        self.assert_refused_by_database(forged)


class TerminateTests(AgentCase):
    def test_terminate(self):
        agent = self.agent()
        agreement = self.agreement(agent, valid_to=D(2030, 12, 31))
        services.terminate(agreement, last_day=D(2026, 1, 31), reason="По соглашению сторон", by=self.director)
        agreement.refresh_from_db()
        self.assertEqual((agreement.terminated_on, agreement.valid_to), (D(2026, 1, 31), D(2030, 12, 31)))
        self.assertEqual(agreement.effective_to, D(2026, 1, 31))
        self.assertFalse(agent.is_working(D(2026, 2, 1)))
        self.assertTrue(ChangeLog.objects.filter(object_id=agreement.pk, field="terminated_on", user=self.director).exists())

    def test_last_day_within_period(self):
        agreement = self.agreement(self.agent(), valid_to=D(2030, 12, 31))
        with self.assertRaises(ValidationError) as caught:
            services.terminate(agreement, last_day=D(2031, 1, 1), reason="X", by=self.director)
        self.assertIn("last_day", caught.exception.message_dict)
        self.assertIsNone(Agreement.objects.get(pk=agreement.pk).terminated_on)

    def test_reason_required_and_only_once(self):
        agreement = self.agreement(self.agent())
        with self.assertRaises(ValidationError):
            services.terminate(agreement, last_day=D(2026, 1, 31), reason="", by=self.director)
        agreement = Agreement.objects.get(pk=agreement.pk)
        services.terminate(agreement, last_day=D(2026, 1, 31), reason="Причина", by=self.director)
        with self.assertRaises(ValidationError):
            services.terminate(agreement, last_day=D(2026, 2, 28), reason="Ещё раз", by=self.director)


class PlacementTests(AgentCase):
    def test_place_and_transfer(self):
        agent = self.agent()
        services.place(agent, department=self.head, valid_from=D(2021, 4, 14), by=self.director)
        services.transfer(agent, department=self.branch, valid_from=D(2025, 1, 1), basis="Приказ 5", by=self.director)
        old, new = agent.placements.order_by("valid_from")
        self.assertEqual((old.department, old.valid_to), (self.head, D(2024, 12, 31)))
        self.assertEqual((new.department, new.valid_from, new.valid_to, new.basis), (self.branch, D(2025, 1, 1), None, "Приказ 5"))
        self.assertEqual(agent.current_placement().department, self.branch)
        self.assertEqual(agent.placements.on(D(2023, 6, 1)).get().department, self.head)  # история

    def test_place_twice_refused(self):
        agent = self.agent()
        services.place(agent, department=self.head, valid_from=D(2021, 4, 14), by=self.director)
        with self.assertRaisesMessage(ValidationError, "Нужен перевод"):
            services.place(agent, department=self.branch, valid_from=D(2025, 1, 1), by=self.director)

    def test_transfer_rules(self):
        agent = self.agent()
        with self.assertRaisesMessage(ValidationError, "нет места"):
            services.transfer(agent, department=self.branch, valid_from=D(2025, 1, 1), by=self.director)
        services.place(agent, department=self.head, valid_from=D(2021, 4, 14), by=self.director)
        with self.assertRaisesMessage(ValidationError, "не раньше следующего дня"):
            services.transfer(agent, department=self.branch, valid_from=D(2021, 4, 14), by=self.director)
        with self.assertRaisesMessage(ValidationError, "уже прикреплён"):
            services.transfer(agent, department=self.head, valid_from=D(2025, 1, 1), by=self.director)

    def test_division_allowed(self):
        agent = self.agent()
        services.place(agent, department=self.division, valid_from=D(2021, 4, 14), by=self.director)
        self.assertEqual(agent.current_placement().department, self.division)

    def test_other_company_department_refused(self):
        agent = self.agent()
        placement = Placement(agent=agent, department=self.other_head, valid_from=D(2021, 1, 1))
        self.assertIn("другой страховой", self.errors(placement)["department"][0])
        self.assert_refused_by_database(lambda: placement.save())

    def test_closed_department_refused(self):
        closed = Department(insurance_company=self.mosaic, kind="division", name="Закрытый", parent=self.head,
                            status=Department.Status.CLOSED)
        closed.save()
        placement = Placement(agent=self.agent(), department=closed, valid_from=D(2021, 1, 1))
        self.assertIn("закрыто", self.errors(placement)["department"][0])

    def test_overlap_refused_in_code_and_database(self):
        agent = self.agent()
        services.place(agent, department=self.head, valid_from=D(2021, 1, 1), by=self.director)
        placement = Placement(agent=agent, department=self.branch, valid_from=D(2022, 1, 1))
        self.assertIn("уже есть место", self.errors(placement)["valid_from"][0])
        self.assert_refused_by_database(lambda: placement.save())

    def test_department_with_agents_cannot_close(self):
        agent = self.agent()
        services.place(agent, department=self.division, valid_from=D(2021, 1, 1), by=self.director)
        self.division.status = Department.Status.CLOSED
        with self.assertRaises(ValidationError) as caught:
            self.division.full_clean()
        self.assertIn("прикреплены агенты", caught.exception.message_dict["status"][0])
        self.division.status = Department.Status.ACTIVE


class AgentErspLinkTests(AgentCase):
    def test_agent_link_and_agency_id(self):
        agent = self.agent()
        agency = ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=70,
            name='Broker: ООО "InRe"', agency_type=2, raw={"id": 70}, synced_at=timezone.now(),
        )
        link = ErspLink(agent=agent, ersp_agency=agency)
        link.full_clean()
        link.save()
        self.assertEqual(link.target(), agent)
        self.assertEqual(agency_id_for_agent(agent, "sandbox"), 70)
        self.assertIsNone(agency_id_for_agent(agent, "prod"))
        second = ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=71,
            name="Другая", agency_type=2, raw={"id": 71}, synced_at=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            ErspLink(agent=agent, ersp_agency=second).full_clean()

    def test_exactly_one_target_in_database(self):
        agent = self.agent()
        agency = ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=72,
            name="X", raw={"id": 72}, synced_at=timezone.now(),
        )
        both = ErspLink(agent=agent, department=self.head, ersp_agency=agency)
        both.normalize()
        self.assert_refused_by_database(lambda: ErspLink._base_manager.bulk_create([both]))

    def test_other_company_agent_refused_by_database(self):
        stranger_agent = Agent(counterparty=self.foreign, area_type_id=URBAN)
        stranger_agent.save()
        agency = ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=73,
            name="X", raw={"id": 73}, synced_at=timezone.now(),
        )
        link = ErspLink(agent=stranger_agent, ersp_agency=agency)
        self.assertIn("другой страховой", self.errors(link)["ersp_agency"][0])
        link.normalize()
        self.assert_refused_by_database(lambda: ErspLink._base_manager.bulk_create([link]))


def page_data(response):
    """
    Данные формы карточки, как их отправил бы браузер без изменений:
    поля карточки и все блоки (с их служебными полями).
    """
    def put(data, name, value):
        if value is None or value is False:
            return
        if value is True:
            data[name] = "on"
        elif isinstance(value, (list, tuple)):
            data[name] = [str(getattr(v, "pk", v)) for v in value]
        elif isinstance(value, datetime.date):
            data[name] = value.isoformat()
        else:
            data[name] = str(getattr(value, "pk", value))

    data = {}
    form = response.context["adminform"].form
    for name, field in form.fields.items():
        if not field.disabled:
            put(data, form.add_prefix(name), form[name].value())
    for inline in response.context["inline_admin_formsets"]:
        formset = inline.formset
        for name in formset.management_form.fields:
            put(data, formset.management_form.add_prefix(name), formset.management_form[name].value())
        for row in formset.initial_forms:
            for name, field in row.fields.items():
                if not field.disabled:
                    put(data, row.add_prefix(name), row[name].value())
    return data


class AgentScreenTests(AgentCase):
    """Экран «Агенты» - от лица обычных сотрудников, не суперпользователя."""

    def login(self, *groups, username="admin_mosaic", company="mosaic"):
        user = User.objects.create_user(
            username, password=STRONG_PASSWORD,
            insurance_company=self.mosaic if company == "mosaic" else None,
        )
        give_roles(user, *groups)
        self.client.force_login(user)
        return user

    def add(self, **data):
        return self.client.post(f"{AGENTS_URL}add/", {
            "counterparty": str(self.inre.pk), "kind": "broker", "area_type": str(URBAN),
            "department": str(self.head.pk), "place_from": "2021-04-01", "place_basis": "",
            "formation_date": "", **data,
        })

    def change_url(self, agent):
        return f"{AGENTS_URL}{agent.pk}/change/"

    def test_add_agent_with_place(self):
        user = self.login("Администратор страховой")
        page = self.add()
        agent = Agent.objects.get(counterparty=self.inre)
        self.assertRedirects(page, self.change_url(agent), fetch_redirect_response=False)
        self.assertEqual((agent.insurance_company, agent.kind, agent.created_by), (self.mosaic, "broker", user))
        placement = agent.placements.get()
        self.assertEqual((placement.department, placement.valid_from, placement.created_by),
                         (self.head, D(2021, 4, 1), user))
        self.assertEqual(agent.formation_date, D(2015, 3, 1))

    def test_add_individual_agent(self):
        self.login("Администратор страховой")
        page = self.add(counterparty=str(self.person_with_data.pk), kind="agent", formation_date="2026-01-15")
        agent = Agent.objects.get(counterparty=self.person_with_data)
        self.assertRedirects(page, self.change_url(agent), fetch_redirect_response=False)
        self.assertEqual(agent.formation_date, D(2026, 1, 15))

    def test_add_individual_agent_without_date_shows_message(self):
        self.login("Администратор страховой")
        page = self.add(counterparty=str(self.person_with_data.pk), kind="agent")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "укажите дату образования вручную")
        self.assertFalse(Agent.objects.exists())

    def test_search_agent_by_pinfl(self):
        agent = self.agent(self.person_with_data, formation_date=D(2026, 1, 1))
        self.agent(self.inre)
        self.login("Администратор страховой")
        found = self.client.get(AGENTS_URL, {"q": "30101990000000"})
        self.assertEqual(list(found.context["cl"].result_list), [agent])

    def test_agents_role_can_work(self):
        self.login("Работа с агентами", username="director_sales")
        self.assertEqual(self.add().status_code, 302)
        self.assertEqual(self.client.get(AGENTS_URL).status_code, 200)

    def test_counterparty_choices(self):
        self.agent(self.moi)
        self.login("Администратор страховой")
        page = self.client.get(f"{AGENTS_URL}add/").content.decode()
        # Только список контрагентов: номера в соседних списках могут совпадать.
        choices = page[page.index('name="counterparty"'):]
        choices = choices[:choices.index("</select>")]
        self.assertIn(f'value="{self.inre.pk}"', choices)
        self.assertNotIn(f'value="{self.moi.pk}"', choices)      # уже агент
        self.assertNotIn(f'value="{self.person.pk}"', choices)   # физлицо без данных
        self.assertIn(f'value="{self.person_with_data.pk}"', choices)  # физлицо с данными
        self.assertIn("FAMILIYA ISM (физлицо)", choices)
        self.assertNotIn(f'value="{self.foreign.pk}"', choices)  # чужая страховая
        self.assertNotIn("Select an option", page)              # пустой пункт - «—»

    def test_other_company_counterparty_refused_with_message(self):
        self.login("Администратор страховой")
        page = self.add(counterparty=str(self.foreign.pk))
        self.assertEqual(page.status_code, 200)
        self.assertFalse(Agent.objects.exists())

    def test_list_only_own_company(self):
        self.agent()
        stranger = Agent(counterparty=self.foreign, area_type_id=URBAN)
        stranger.save()
        self.login("Администратор страховой")
        page = self.client.get(AGENTS_URL)
        self.assertContains(page, "InRe")
        self.assertNotContains(page, "Чужая организация")
        self.assertEqual(self.client.get(self.change_url(stranger)).status_code, 302)

    def test_add_agreement_on_card(self):
        agent = self.agent()
        user = self.login("Администратор страховой")
        data = page_data(self.client.get(self.change_url(agent)))
        data.update({
            "agreements-TOTAL_FORMS": "1",
            "agreements-0-number": "AZM/21-000", "agreements-0-signed_on": "2021-04-14",
            "agreements-0-valid_from": "2021-04-14", "agreements-0-signer_name": "Халилов О.Н.",
            "agreements-0-signer_position": "Генеральный директор", "agreements-0-signer_basis": "charter",
            "agreements-0-agent": str(agent.pk),
        })
        page = self.client.post(self.change_url(agent), data)
        self.assertEqual(page.status_code, 302, page.content.decode()[:3000])
        agreement = agent.agreements.get()
        self.assertEqual((agreement.number, agreement.created_by), ("AZM/21-000", user))
        history = self.client.get(f"{AGENTS_URL}{agent.pk}/history/", follow=True)
        self.assertContains(history, "AZM/21-000")

    def test_agreement_errors_are_messages_not_500(self):
        agent = self.agent()
        self.agreement(agent)  # бессрочное с 14.04.2021
        self.login("Администратор страховой")
        data = page_data(self.client.get(self.change_url(agent)))
        base = {
            "agreements-TOTAL_FORMS": "2", "agreements-1-agent": str(agent.pk),
            "agreements-1-signed_on": "2022-01-01", "agreements-1-valid_from": "2022-01-01",
            "agreements-1-signer_basis": "charter",
        }
        # Пересечение с действующим соглашением («прекращено» на экране только для чтения).
        page = self.client.post(self.change_url(agent), {
            **data, **base, "agreements-1-number": "2", "agreements-1-signer_name": "Халилов",
        })
        self.assertContains(page, "уже есть соглашение")
        # Подписант не указан; подписант не работал на дату.
        page = self.client.post(self.change_url(agent), {**data, **base, "agreements-1-number": "3"})
        self.assertContains(page, "кто подписал")
        page = self.client.post(self.change_url(agent), {
            **data, **base, "agreements-1-number": "4", "agreements-1-signed_by": str(self.newcomer.pk),
        })
        self.assertContains(page, "не работал(а) в страховой 01.01.2022")
        # Повтор номера в страховой: страховой в форме нет, проверяет clean().
        page = self.client.post(self.change_url(agent), {
            **data, **base, "agreements-1-number": "AZM/21-000", "agreements-1-signer_name": "Халилов",
            "agreements-1-signed_on": "2030-01-01", "agreements-1-valid_from": "2030-01-01",
        })
        self.assertContains(page, "№ AZM/21-000 в страховой уже есть")
        self.assertEqual(agent.agreements.count(), 1)

    def test_signer_choices_own_staff_only(self):
        agent = self.agent()
        self.login("Администратор страховой")
        page = self.client.get(self.change_url(agent)).content.decode()
        choices = page[page.index('name="agreements-__prefix__-signed_by"'):]
        choices = choices[:choices.index("</select>")]
        self.assertIn(f'value="{self.director.pk}"', choices)
        self.assertNotIn(f'value="{self.stranger.pk}"', choices)

    def test_buttons_and_transfer(self):
        agent = self.agent()
        services.place(agent, department=self.head, valid_from=D(2021, 4, 1), by=self.director)
        user = self.login("Администратор страховой")
        page = self.client.get(self.change_url(agent))
        self.assertContains(page, f"{AGENTS_URL}{agent.pk}/transfer/")
        self.assertNotContains(page, f"{AGENTS_URL}{agent.pk}/terminate/")  # соглашения нет
        page = self.client.post(f"{AGENTS_URL}{agent.pk}/transfer/", {
            "department": str(self.branch.pk), "valid_from": "2025-01-01", "basis": "Приказ 7",
        })
        self.assertRedirects(page, self.change_url(agent), fetch_redirect_response=False)
        self.assertEqual(agent.current_placement().department, self.branch)
        self.assertEqual(agent.current_placement().created_by, user)

    def test_transfer_error_shown(self):
        agent = self.agent()
        services.place(agent, department=self.head, valid_from=D(2021, 4, 1), by=self.director)
        self.login("Администратор страховой")
        page = self.client.post(f"{AGENTS_URL}{agent.pk}/transfer/", {
            "department": str(self.branch.pk), "valid_from": "2021-04-01",
        })
        self.assertContains(page, "не раньше следующего дня")

    def test_place_button_when_no_place(self):
        agent = self.agent()
        self.login("Администратор страховой")
        self.assertContains(self.client.get(self.change_url(agent)), f"{AGENTS_URL}{agent.pk}/place/")
        page = self.client.post(f"{AGENTS_URL}{agent.pk}/place/", {
            "department": str(self.division.pk), "valid_from": "2021-04-01",
        })
        self.assertEqual(page.status_code, 302)
        self.assertEqual(agent.current_placement().department, self.division)

    def test_terminate_on_screen_with_ersp_reminder(self):
        agent = self.agent()
        agreement = self.agreement(agent)
        agency = ErspAgency.objects.create(
            insurance_company=self.mosaic, environment=Environment.SANDBOX, ersp_id=70,
            name="Broker", raw={"id": 70}, synced_at=timezone.now(),
        )
        ErspLink(agent=agent, ersp_agency=agency).save()
        self.login("Администратор страховой")
        page = self.client.get(f"{AGENTS_URL}{agent.pk}/terminate/")
        self.assertContains(page, "«Delete»")
        self.assertContains(page, "agencyId 70 (песочница)")
        page = self.client.post(f"{AGENTS_URL}{agent.pk}/terminate/", {"last_day": "2026-01-31", "reason": "Конец"})
        self.assertEqual(page.status_code, 302)
        agreement.refresh_from_db()
        self.assertEqual((agreement.terminated_on, agreement.termination_reason), (D(2026, 1, 31), "Конец"))

    def test_employee_and_platform_refused(self):
        agent = self.agent()
        self.login("Сотрудник", username="clerk")
        with refusal_expected(self):
            self.assertEqual(self.client.get(AGENTS_URL).status_code, 403)
        with refusal_expected(self):
            self.assertEqual(self.client.post(f"{AGENTS_URL}{agent.pk}/terminate/", {}).status_code, 403)
        self.login("Администратор платформы", username="platform", company=None)
        with refusal_expected(self):
            self.assertEqual(self.client.get(AGENTS_URL).status_code, 403)

    def test_no_delete(self):
        agent = self.agent()
        self.login("Администратор страховой")
        with refusal_expected(self):
            self.assertEqual(self.client.get(f"{AGENTS_URL}{agent.pk}/delete/").status_code, 403)

    def test_role_rights(self):
        from django.contrib.auth.models import Group

        rights = set(Group.objects.get(name="Работа с агентами").permissions.values_list("codename", flat=True))
        self.assertTrue({"add_agent", "change_agent", "add_agreement", "change_placement", "add_ersplink",
                         "view_counterparty", "view_erspagency"} <= rights)
        self.assertFalse({r for r in rights if r.startswith("delete_")})
        self.assertNotIn("change_counterparty", rights)
        admin_rights = set(Group.objects.get(name="Администратор страховой").permissions.values_list("codename", flat=True))
        self.assertTrue({"add_agent", "change_agreement", "add_placement"} <= admin_rights)
        platform = set(Group.objects.get(name="Администратор платформы").permissions.values_list("codename", flat=True))
        self.assertFalse({r for r in platform if r.endswith(("_agent", "_agreement", "_placement", "_ersplink"))})
