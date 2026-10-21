"""
Агенты страховой и связь наших записей с ЕРСП (модель: docs/users-model.md,
раздел «Агенты (этап B)»).

Агент - организация (позже и человек), которая продаёт полисы страховой
по соглашению. В системе агент = контрагент из справочника (Agent) +
соглашения (Agreement) + место в подразделении (Placement) + связь с ЕРСП
(ErspLink) + пользователи для входа (Member, часть B3). Брокеры ведутся здесь же
с видом «брокер» (решение 30). Сейчас - только юрлица (решение 22): ИП -
физлицо (решение 29), они появятся после этапа «Физлица».

Ничего не удаляется: соглашение прекращают датой, место закрывают датой,
связь с ЕРСП снимают отметкой. Агент «работает», пока у него есть
действующее соглашение - это не хранится, а считается (is_working).

agencyId уходит в ЕРСП в каждом договоре, полисе, подтверждении оплаты:
продал сотрудник - agencyId его подразделения (у отдела - его головного
офиса или филиала: отделов в ЕРСП нет), продал агент - agencyId агента.
В песочнице и в боевой среде id разные, поэтому связь - своя в каждой
среде (среда берётся из записи справочника ЕРСП).

Правила, которые смотрят на другие строки (своя страховая, «не меняется
после создания»), держит база - триггеры в миграции agents/0004; они же
продублированы в clean(): у формы нет страховой, без этого сотрудник
получил бы ошибку 500 вместо сообщения (ловушка из AGENTS.md).
"""
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeBoundary, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import Assignment, DateRange, User
from accounts.visibility import Kind
from audit.models import TrackedModel, TrackedQuerySet
from companies.models import Department
from counterparties.models import BankAccount, Counterparty
from integrations.models import Environment, ErspAgency
from references.models import ErspAreaType

_INCLUSIVE = RangeBoundary(inclusive_lower=True, inclusive_upper=True)


def _squash(value):
    return " ".join((value or "").split())


def _overlapping(rows, start, end):
    """
    Первая из записей rows, чей период (start, effective_end) пересекается
    с [start, end] (end None - без конца). Повторяет ограничение исключения
    базы: форма его сама не проверит, если одно из полей только для чтения.
    """
    for row in rows:
        row_start, row_end = row
        if (end is None or row_start <= end) and (row_end is None or start <= row_end):
            return row
    return None


def _fixed(instance, *attnames):
    """Поля, изменённые относительно базы (у новой записи - ничего)."""
    if instance.pk is None:
        return []
    before = type(instance).objects.filter(pk=instance.pk).values(*attnames).first() or {}
    return [name for name in attnames if name in before and before[name] != getattr(instance, name)]


# --- Агент -----------------------------------------------------------------------


class Agent(TrackedModel):
    """
    Агент или брокер страховой: карточка поверх контрагента из справочника.

    Один контрагент - не больше одного агента в страховой. Страховая
    и контрагент после создания не меняются (проверяет и база).
    Данные для ЕРСП (тип местности, дата образования, «электронный»)
    хранятся здесь: в ЕРСП они обязательны при регистрации агента.
    """

    class Kind(models.TextChoices):
        AGENT = "agent", "агент"
        BROKER = "broker", "брокер"

    insurance_company = models.ForeignKey(
        "companies.InsuranceCompany",
        on_delete=models.PROTECT,
        related_name="agents",
        verbose_name="страховая компания",
        editable=False,
    )
    counterparty = models.ForeignKey(
        Counterparty,
        on_delete=models.PROTECT,
        related_name="agents",
        verbose_name="контрагент",
        help_text="Организация из справочника контрагентов своей страховой. После создания не меняется.",
    )
    kind = models.CharField("вид", max_length=16, choices=Kind.choices, default=Kind.AGENT)
    area_type = models.ForeignKey(
        ErspAreaType,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="тип местности",
    )
    formation_date = models.DateField(
        "дата образования", null=True, blank=True,
        help_text="В ЕРСП обязательна. Пусто - система возьмёт дату свидетельства о регистрации юрлица.",
    )
    is_electronic = models.BooleanField("электронный агент", default=False)
    is_head_electronic = models.BooleanField("головной электронный", default=False)
    head_agent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="child_agents",
        verbose_name="входит в электронного",
        help_text="Только у электронного агента, не головного: его головной электронный агент.",
    )
    commission_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="счёт для комиссии",
        help_text="Счёт этого контрагента. В ЕРСП уходит как расчётный счёт.",
    )

    class Meta:
        verbose_name = "агент"
        verbose_name_plural = "агенты"
        ordering = ["counterparty__display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["insurance_company", "counterparty"],
                name="agents_agent_one_per_counterparty",
                violation_error_message="Этот контрагент уже заведён агентом.",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["agent", "broker"]),
                name="agents_agent_kind_known",
            ),
            models.CheckConstraint(
                condition=Q(is_head_electronic=False) | Q(is_electronic=True),
                name="agents_agent_head_is_electronic",
                violation_error_message="Головным электронным бывает только электронный агент.",
            ),
            models.CheckConstraint(
                condition=Q(head_agent__isnull=True) | Q(is_electronic=True, is_head_electronic=False),
                name="agents_agent_head_agent_only_for_electronic",
                violation_error_message="«Входит в электронного» - только у электронного агента, не головного.",
            ),
            models.CheckConstraint(
                condition=~Q(head_agent=F("id")),
                name="agents_agent_not_own_head",
                violation_error_message="Агент не может входить сам в себя.",
            ),
        ]

    def __str__(self):
        if self.counterparty_id:
            return str(self.counterparty)
        return f"Агент № {self.pk}"

    def normalize(self):
        if self.counterparty_id and not self.insurance_company_id:
            self.insurance_company_id = self.counterparty.insurance_company_id
        if not self.is_electronic:
            self.is_head_electronic = False
        if not self.is_electronic or self.is_head_electronic:
            self.head_agent = None
        if self.formation_date is None and self.counterparty_id:
            legal = getattr(self.counterparty, "legal_entity", None)
            if legal is not None and legal.reg_certificate_date:
                self.formation_date = legal.reg_certificate_date

    def clean(self):
        self.normalize()
        errors = {}
        counterparty = self.counterparty if self.counterparty_id else None
        company_id = self.insurance_company_id

        if "counterparty_id" in _fixed(self, "counterparty_id"):
            errors["counterparty"] = "Контрагент агента не меняется. Заведите нового агента."
        if counterparty is not None:
            if company_id and counterparty.insurance_company_id != company_id:
                errors["counterparty"] = "Это контрагент другой страховой."
            elif counterparty.kind != Counterparty.Kind.LEGAL_ENTITY:
                errors["counterparty"] = (
                    "Сейчас агентом заводится только юрлицо. Физлица и ИП - после этапа «Физлица»."
                )
            elif self.pk is None and company_id and type(self).objects.filter(
                insurance_company_id=company_id, counterparty=counterparty
            ).exists():
                errors["counterparty"] = "Этот контрагент уже заведён агентом."

        if self.head_agent_id is not None:
            head = self.head_agent
            if self.pk is not None and head.pk == self.pk:
                errors["head_agent"] = "Агент не может входить сам в себя."
            elif head.insurance_company_id != company_id:
                errors["head_agent"] = "Это агент другой страховой."
            elif not head.is_head_electronic:
                errors["head_agent"] = f"«{head}» - не головной электронный агент."

        if self.commission_account_id is not None and self.commission_account.counterparty_id != self.counterparty_id:
            errors["commission_account"] = "Это счёт другого контрагента."

        if errors:
            raise ValidationError(errors)

    def current_agreement(self, day=None):
        return self.agreements.on(day).first()

    def current_placement(self, day=None):
        return self.placements.on(day).select_related("department__parent").first()

    def is_working(self, day=None):
        """Работает ли агент на дату: есть действующее соглашение."""
        return self.agreements.on(day).exists()


# --- Соглашение --------------------------------------------------------------------


class PeriodQuerySet(TrackedQuerySet):
    """Записи «с - по» (по включительно, пусто - без конца)."""

    end_field = "valid_to"

    def on(self, day=None):
        """Действующие на дату (по умолчанию - сегодня по часовому поясу системы)."""
        day = day or timezone.localdate()
        return self.filter(
            Q(**{f"{self.end_field}__isnull": True}) | Q(**{f"{self.end_field}__gte": day}),
            valid_from__lte=day,
        )


class AgreementQuerySet(PeriodQuerySet):
    def on(self, day=None):
        # Действует с «с» по «прекращено» (если задано), иначе по «по».
        day = day or timezone.localdate()
        return self.annotate(effective_end=Coalesce("terminated_on", "valid_to")).filter(
            Q(effective_end__isnull=True) | Q(effective_end__gte=day), valid_from__lte=day,
        )


class Agreement(TrackedModel):
    """
    Соглашение страховой с агентом (агентский договор).

    У одного агента периоды соглашений не пересекаются (ограничение
    исключения в базе, как у прикреплений); продление - новое соглашение.
    Досрочное прекращение - датой последнего дня и причиной (кнопка
    «Прекратить соглашение»), «по» при этом остаётся как было в договоре.

    Подписал от страховой (решение 35): пользователь своей страховой,
    работавший на дату заключения, - или, если такого пользователя
    в системе нет (старое соглашение, бывший директор), ФИО и должность
    текстом. Одно из двух обязательно.
    """

    class SignerBasis(models.TextChoices):
        CHARTER = "charter", "устав"
        BRANCH_REGULATION = "branch_regulation", "положение о филиале"
        POWER_OF_ATTORNEY = "power_of_attorney", "доверенность"

    agent = models.ForeignKey(Agent, on_delete=models.PROTECT, related_name="agreements", verbose_name="агент")
    insurance_company = models.ForeignKey(
        "companies.InsuranceCompany", on_delete=models.PROTECT, related_name="+",
        verbose_name="страховая компания", editable=False,
    )
    number = models.CharField("номер", max_length=255)
    signed_on = models.DateField("дата заключения")
    valid_from = models.DateField("действует с")
    # У полей, стоящих на экране парой, пояснений нет («ровность полей», AGENTS.md):
    # всё важное - в подсказке у поля «подписал» (agents/admin.py).
    valid_to = models.DateField("действует по", null=True, blank=True)
    terminated_on = models.DateField("прекращено", null=True, blank=True)
    termination_reason = models.CharField("причина прекращения", max_length=500, blank=True)
    signed_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        verbose_name="подписал (пользователь)",
        help_text="Сотрудник своей страховой, работавший на дату заключения.",
    )
    signer_name = models.CharField("подписал (ФИО)", max_length=255, blank=True)
    signer_position = models.CharField("должность подписанта", max_length=255, blank=True)
    signer_basis = models.CharField("основание подписи", max_length=32, choices=SignerBasis.choices)
    power_of_attorney_number = models.CharField("доверенность: номер", max_length=100, blank=True)
    power_of_attorney_date = models.DateField("доверенность: дата", null=True, blank=True)
    # Текущий скан (PDF; в ЕРСП уходит он). Прежние сканы остаются в хранилище
    # файлов и открываются из страницы «Скан» (docs/files-model.md). Ставит только
    # services.attach_scan: файл должен быть именно этого соглашения (держит и база).
    scan = models.ForeignKey(
        "files.StoredFile", on_delete=models.PROTECT, null=True, blank=True, editable=False,
        related_name="+", verbose_name="скан",
    )

    objects = AgreementQuerySet.as_manager()

    class Meta:
        verbose_name = "соглашение"
        verbose_name_plural = "соглашения"
        ordering = ["agent", "-valid_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["insurance_company", "number"],
                name="agents_agreement_number_unique",
                violation_error_message="Соглашение с таким номером в страховой уже есть.",
            ),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="agents_agreement_dates_order",
                violation_error_message="«Действует по» не может быть раньше «действует с».",
            ),
            models.CheckConstraint(
                condition=Q(terminated_on__isnull=True)
                | (Q(terminated_on__gte=F("valid_from"))
                   & (Q(valid_to__isnull=True) | Q(terminated_on__lte=F("valid_to")))),
                name="agents_agreement_termination_in_period",
                violation_error_message="Дата прекращения - в пределах срока соглашения.",
            ),
            models.CheckConstraint(
                condition=(Q(terminated_on__isnull=True) & Q(termination_reason=""))
                | (Q(terminated_on__isnull=False) & ~Q(termination_reason="")),
                name="agents_agreement_termination_reason",
                violation_error_message="Причина прекращения указывается вместе с датой прекращения.",
            ),
            models.CheckConstraint(
                condition=Q(signed_by__isnull=False) | ~Q(signer_name=""),
                name="agents_agreement_has_signer",
                violation_error_message="Укажите, кто подписал: пользователя или ФИО.",
            ),
            models.CheckConstraint(
                condition=Q(signer_basis__in=["charter", "branch_regulation", "power_of_attorney"]),
                name="agents_agreement_signer_basis_known",
            ),
            models.CheckConstraint(
                condition=(Q(signer_basis="power_of_attorney") & ~Q(power_of_attorney_number="")
                           & Q(power_of_attorney_date__isnull=False))
                | (~Q(signer_basis="power_of_attorney") & Q(power_of_attorney_number="")
                   & Q(power_of_attorney_date__isnull=True)),
                name="agents_agreement_power_of_attorney",
                violation_error_message="Номер и дата доверенности - при основании «доверенность», и только при нём.",
            ),
            ExclusionConstraint(
                name="agents_agreement_no_overlap",
                expressions=[
                    ("agent", RangeOperators.EQUAL),
                    (DateRange("valid_from", Coalesce("terminated_on", "valid_to"), _INCLUSIVE),
                     RangeOperators.OVERLAPS),
                ],
                violation_error_message="На эти даты у агента уже есть другое соглашение.",
            ),
        ]

    def __str__(self):
        since = f" от {self.signed_on:%d.%m.%Y}" if self.signed_on else ""
        return f"№ {self.number}{since}"

    def validate_constraints(self, exclude=None):
        # «Периоды не пересекаются» Django проверить не может: подставляет пустое
        # «прекращено» в запрос без типа, и PostgreSQL отвечает ошибкой. Эту проверку
        # делает clean() (те же правила, с понятным сообщением), при сохранении
        # её держит база (ограничение agents_agreement_no_overlap).
        # «Номер не повторяется в страховой» - тоже в clean(): на экране страховой
        # в форме нет, и Django это правило сам не проверил бы.
        super().validate_constraints(exclude={*(exclude or ()), "terminated_on", "insurance_company"})

    def journal_root(self):
        return self.agent if self.agent_id else None

    @property
    def effective_to(self):
        """Последний день действия: прекращено (если задано), иначе «по»."""
        return self.terminated_on or self.valid_to

    def signer(self):
        if self.signed_by_id:
            return self.signed_by.full_name()
        position = f", {self.signer_position}" if self.signer_position else ""
        return f"{self.signer_name}{position}"

    signer.short_description = "подписал"

    def normalize(self):
        if self.agent_id:
            self.insurance_company_id = self.agent.insurance_company_id
        self.number = _squash(self.number)
        self.signer_name = _squash(self.signer_name)
        self.signer_position = _squash(self.signer_position)
        self.termination_reason = _squash(self.termination_reason)
        self.power_of_attorney_number = _squash(self.power_of_attorney_number)
        if self.signed_by_id:
            # Подписант - пользователь системы: ФИО и должность берутся из его записи.
            self.signer_name = ""
            self.signer_position = ""
        if self.signer_basis != self.SignerBasis.POWER_OF_ATTORNEY:
            self.power_of_attorney_number = ""
            self.power_of_attorney_date = None

    def clean(self):
        self.normalize()
        errors = {}
        agent = self.agent if self.agent_id else None

        if "agent_id" in _fixed(self, "agent_id"):
            errors["agent"] = "Соглашение не переносится на другого агента."

        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            # Ошибка здесь заодно выключает проверку пересечений с базой: с такими
            # датами PostgreSQL не построит период и ответил бы ошибкой (500).
            errors["valid_to"] = "«Действует по» не может быть раньше «действует с»."
        if self.terminated_on and self.valid_from and (
            self.terminated_on < self.valid_from or (self.valid_to and self.terminated_on > self.valid_to)
        ):
            errors["terminated_on"] = "Дата прекращения - в пределах срока соглашения."
        if bool(self.terminated_on) != bool(self.termination_reason):
            errors["termination_reason"] = "Причина прекращения указывается вместе с датой прекращения."

        if self.signer_basis == self.SignerBasis.POWER_OF_ATTORNEY:
            if not self.power_of_attorney_number:
                errors["power_of_attorney_number"] = "Укажите номер доверенности."
            if not self.power_of_attorney_date:
                errors["power_of_attorney_date"] = "Укажите дату доверенности."

        if self.signed_by_id is None:
            if not self.signer_name:
                errors["signed_by"] = (
                    "Укажите, кто подписал: пользователя системы или, если его в системе нет, ФИО."
                )
        elif agent is not None:
            signer = self.signed_by
            if signer.insurance_company_id != agent.insurance_company_id:
                errors["signed_by"] = "Подписант - из другой страховой."
            elif signer.kind not in (User.Kind.EMPLOYEE, User.Kind.CONTRACTOR):
                errors["signed_by"] = "Подписывает сотрудник страховой (штатный или по ГПХ)."
            elif self.signed_on and not Assignment.objects.on(self.signed_on).filter(user=signer).exists():
                errors["signed_by"] = (
                    f"{signer.full_name()} не работал(а) в страховой {self.signed_on:%d.%m.%Y}. "
                    "Если подписант в системе не заведён на эту дату - оставьте поле пустым "
                    "и впишите ФИО и должность."
                )

        if agent is not None and self.number and type(self).objects.filter(
            insurance_company_id=agent.insurance_company_id, number=self.number,
        ).exclude(pk=self.pk).exists():
            errors["number"] = f"Соглашение № {self.number} в страховой уже есть."

        if agent is not None and self.valid_from and "valid_to" not in errors and "terminated_on" not in errors:
            others = type(self).objects.filter(agent=agent).exclude(pk=self.pk)
            periods = {(o.valid_from, o.effective_to): o for o in others}
            hit = _overlapping(periods, self.valid_from, self.effective_to)
            if hit is not None:
                errors["valid_from"] = f"На эти даты у агента уже есть соглашение {periods[hit]}."

        if agent is not None and self.pk is None and agent.counterparty.status != Counterparty.Status.ACTIVE:
            errors["agent"] = f"Контрагент «{agent.counterparty}» в архиве - новое соглашение не заключить."

        if errors:
            raise ValidationError(errors)


# --- Место агента ------------------------------------------------------------------


class Placement(TrackedModel):
    """
    Место агента: к какому подразделению он прикреплён с какой даты по какую.

    Периоды одного агента не пересекаются (база). Перевод - кнопкой
    «Перевести»: старое место закрывается накануне, новое открывается
    с даты. Подразделение и даты не правятся задним числом: по этой
    таблице номер договора возьмёт код подразделения на дату продажи,
    а ЕРСП - parentId агента (у отдела - его головной офис или филиал).
    """

    agent = models.ForeignKey(Agent, on_delete=models.PROTECT, related_name="placements", verbose_name="агент")
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name="agent_placements", verbose_name="подразделение",
    )
    valid_from = models.DateField("действует с")
    valid_to = models.DateField("действует по", null=True, blank=True, help_text="Включительно. Пусто - по сей день.")
    basis = models.CharField("основание", max_length=255, blank=True)

    objects = PeriodQuerySet.as_manager()

    class Meta:
        verbose_name = "место агента"
        verbose_name_plural = "места агента"
        ordering = ["agent", "-valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="agents_placement_dates_order",
                violation_error_message="«Действует по» не может быть раньше «действует с».",
            ),
            ExclusionConstraint(
                name="agents_placement_no_overlap",
                expressions=[
                    ("agent", RangeOperators.EQUAL),
                    (DateRange("valid_from", "valid_to", _INCLUSIVE), RangeOperators.OVERLAPS),
                ],
                violation_error_message="На эти даты у агента уже есть другое место.",
            ),
        ]

    def __str__(self):
        department = self.department.full_name() if self.department_id else "—"
        since = f", с {self.valid_from:%d.%m.%Y}" if self.valid_from else ""
        return f"{department}{since}"

    @property
    def insurance_company_id(self):
        # Для журнала изменений: строка журнала видна только своей страховой.
        return self.agent.insurance_company_id if self.agent_id else None

    def journal_root(self):
        return self.agent if self.agent_id else None

    def normalize(self):
        self.basis = _squash(self.basis)

    def clean(self):
        self.normalize()
        errors = {}
        agent = self.agent if self.agent_id else None
        department = self.department if self.department_id else None

        if "agent_id" in _fixed(self, "agent_id"):
            errors["agent"] = "Место не переносится на другого агента."
        if department is not None and agent is not None:
            if department.insurance_company_id != agent.insurance_company_id:
                errors["department"] = "Это подразделение другой страховой."
            elif department.status != Department.Status.ACTIVE and (
                self.pk is None or _fixed(self, "department_id")
            ):
                errors["department"] = f"Подразделение «{department.name}» закрыто."
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            errors["valid_to"] = "«Действует по» не может быть раньше «действует с»."
        elif agent is not None and self.valid_from:
            others = type(self).objects.filter(agent=agent).exclude(pk=self.pk).select_related("department")
            periods = {(o.valid_from, o.valid_to): o for o in others}
            hit = _overlapping(periods, self.valid_from, self.valid_to)
            if hit is not None:
                errors["valid_from"] = f"На эти даты у агента уже есть место: {periods[hit]}."

        if errors:
            raise ValidationError(errors)


# --- Пользователь агента -------------------------------------------------------------

AGENT_ROLE = "agent"               # роль «Агент»: свои продажи
AGENT_DIRECTOR_ROLE = "agent_director"  # роль «Директор агента»: продажи всего агентства


class Member(TrackedModel):
    """
    Пользователь агента: учётная запись вида «агент», привязанная к агенту
    с даты по дату. Каждый сотрудник агентства входит под своим логином.

    Директор агента видит все продажи агентства, обычный сотрудник - только
    свои (роли «Директор агента» и «Агент»; права в них придут вместе
    с договорами). У одного пользователя периоды не пересекаются (база).
    Пользователь и агент после создания не меняются (триггер).

    Заводит и отключает пользователя агента страховая (решение 28), из
    карточки агента. Вход - только пока у пользователя действующая связь
    с агентом, а у агента - действующее соглашение (accounts/backends.py).
    """

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="agent_memberships", verbose_name="пользователь")
    agent = models.ForeignKey(Agent, on_delete=models.PROTECT, related_name="members", verbose_name="агент")
    is_director = models.BooleanField(
        "директор агента", default=False,
        help_text="Видит все продажи агентства. Обычный сотрудник агента - только свои.",
    )
    valid_from = models.DateField("действует с")
    valid_to = models.DateField("действует по", null=True, blank=True)

    objects = PeriodQuerySet.as_manager()

    class Meta:
        verbose_name = "пользователь агента"
        verbose_name_plural = "пользователи агента"
        ordering = ["agent", "-valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="agents_member_dates_order",
                violation_error_message="«Действует по» не может быть раньше «действует с».",
            ),
            ExclusionConstraint(
                name="agents_member_no_overlap",
                expressions=[
                    ("user", RangeOperators.EQUAL),
                    (DateRange("valid_from", "valid_to", _INCLUSIVE), RangeOperators.OVERLAPS),
                ],
                violation_error_message="На эти даты пользователь уже связан с агентом.",
            ),
        ]

    def __str__(self):
        user = self.user.full_name() if self.user_id else "?"
        return f"{user}{' (директор)' if self.is_director else ''}"

    @property
    def insurance_company_id(self):
        # Для журнала изменений: строка журнала видна только своей страховой.
        return self.agent.insurance_company_id if self.agent_id else None

    def journal_root(self):
        return self.agent if self.agent_id else None

    def role_code(self):
        return AGENT_DIRECTOR_ROLE if self.is_director else AGENT_ROLE

    def clean(self):
        errors = {}
        user = self.user if self.user_id else None
        agent = self.agent if self.agent_id else None
        if _fixed(self, "user_id", "agent_id"):
            errors["user"] = "Связь не переносится на другого пользователя или агента."
        if user is not None:
            if user.kind != User.Kind.AGENT:
                errors["user"] = "Пользователь агента - только учётная запись вида «агент»."
            elif agent is not None and user.insurance_company_id != agent.insurance_company_id:
                errors["user"] = "Пользователь - из другой страховой."
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            errors["valid_to"] = "«Действует по» не может быть раньше «действует с»."
        elif user is not None and self.valid_from:
            others = type(self).objects.filter(user=user).exclude(pk=self.pk)
            periods = {(o.valid_from, o.valid_to): o for o in others}
            if _overlapping(periods, self.valid_from, self.valid_to) is not None:
                errors["valid_from"] = "На эти даты пользователь уже связан с агентом."
        if errors:
            raise ValidationError(errors)


def current_membership(user, day=None):
    """Действующая на дату связь пользователя с агентом или None."""
    return user.agent_memberships.on(day).select_related("agent").first()


def agent_user_may_work(user, day=None):
    """
    Может ли пользователь агента войти на дату: есть действующая связь
    с агентом, и у агента - действующее соглашение. Соглашение закончилось -
    вход закрывается сам, без ночных заданий.
    """
    membership = current_membership(user, day)
    return membership is not None and membership.agent.is_working(day)


# --- Связь с ЕРСП ------------------------------------------------------------------


class ErspLink(TrackedModel):
    """
    Связь нашей записи с записью справочника ЕРСП «Агенты/филиалы»:
    ровно одно из двух - подразделение или агент.

    Правила (в базе - ограничения и триггер agents_ersplink_rules, они же
    в clean(): у формы нет страховой и среды, без этого была бы ошибка 500):
    - одна запись ЕРСП - одна наша запись, навсегда (связь не переносится:
      по ней видно, с каким agencyId уходили договоры);
    - у подразделения и у агента в каждой среде - не больше одной
      действующей связи;
    - связывается головной офис или филиал (отдел берёт связь своего
      родителя) или агент своей страховой, с записью ЕРСП своей страховой;
    - новую связь нельзя сделать с записью, удалённой в ЕРСП или пропавшей
      из него; запись ЕРСП, подразделение, агент, страховая и среда после
      создания не меняются.
    Связь не удаляется: если запись в ЕРСП удалили и завели новую, старую
    связь снимают (отметка «действует»), новую заводят.
    """

    insurance_company = models.ForeignKey(
        "companies.InsuranceCompany",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="страховая компания",
        editable=False,
    )
    environment = models.CharField(
        "среда", max_length=16, choices=Environment.choices, editable=False,
        help_text="Берётся из записи ЕРСП.",
    )
    ersp_agency = models.OneToOneField(
        ErspAgency,
        on_delete=models.PROTECT,
        related_name="link",
        verbose_name="запись ЕРСП (agencyId)",
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="ersp_links",
        verbose_name="подразделение",
        null=True,
        blank=True,
    )
    agent = models.ForeignKey(
        Agent,
        on_delete=models.PROTECT,
        related_name="ersp_links",
        verbose_name="агент",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(
        "действует", default=True,
        help_text="Снимите, если запись в ЕРСП удалили или заменили: связь останется в истории.",
    )

    class Meta:
        verbose_name = "связь с ЕРСП"
        verbose_name_plural = "связи с ЕРСП"
        ordering = ["environment", "ersp_agency__ersp_id"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(department__isnull=False) & Q(agent__isnull=True))
                | (Q(department__isnull=True) & Q(agent__isnull=False)),
                name="agents_ersplink_has_target",
                violation_error_message="Запись ЕРСП связывается либо с подразделением, либо с агентом.",
            ),
            models.CheckConstraint(
                condition=Q(environment__in=["prod", "sandbox"]),
                name="agents_ersplink_environment_known",
            ),
            models.UniqueConstraint(
                fields=["department", "environment"],
                condition=Q(is_active=True),
                name="agents_ersplink_one_per_department",
                violation_error_message="У подразделения в этой среде уже есть действующая связь с ЕРСП.",
            ),
            models.UniqueConstraint(
                fields=["agent", "environment"],
                condition=Q(is_active=True),
                name="agents_ersplink_one_per_agent",
                violation_error_message="У агента в этой среде уже есть действующая связь с ЕРСП.",
            ),
        ]

    def __str__(self):
        agency = self.ersp_agency if self.ersp_agency_id else None
        return f"{agency.ersp_id if agency else '?'} ({self.get_environment_display()})"

    def target(self):
        """С чем связана запись ЕРСП."""
        return self.department if self.department_id else self.agent

    def journal_root(self):
        # Связь видна в «Истории» подразделения или агента.
        return self.target()

    def normalize(self):
        # Страховая и среда - всегда от записи ЕРСП: их нет в форме.
        if self.ersp_agency_id:
            self.insurance_company_id = self.ersp_agency.insurance_company_id
            self.environment = self.ersp_agency.environment

    def clean(self):
        self.normalize()
        errors = {}
        agency = self.ersp_agency if self.ersp_agency_id else None
        target = self.target() if (self.department_id or self.agent_id) else None

        if _fixed(self, "ersp_agency_id", "department_id", "agent_id"):
            errors["ersp_agency"] = (
                "Связь не переносится. Снимите отметку «действует» и заведите новую связь."
            )

        if self.department_id and self.department.kind == Department.Kind.DIVISION:
            errors["department"] = (
                "Отдел с ЕРСП не связывается: в ЕРСП отделов нет, он берёт связь своего "
                "головного офиса или филиала."
            )
        elif target is not None and agency is not None and target.insurance_company_id != agency.insurance_company_id:
            errors["ersp_agency"] = "Это запись ЕРСП другой страховой."

        if agency is not None and self.is_active and (self.pk is None or self._activated()):
            if agency.is_deleted:
                errors["ersp_agency"] = f"Запись {agency.ersp_id} удалена в ЕРСП - её agencyId больше не принимается."
            elif not agency.is_present:
                errors["ersp_agency"] = (
                    f"Записи {agency.ersp_id} нет в последней загрузке справочника ЕРСП. "
                    "Загрузите справочник заново (load_ersp_agencies)."
                )

        if target is not None and agency is not None and self.is_active and "department" not in errors:
            field = "department" if self.department_id else "agent"
            taken = type(self).objects.filter(
                **{field: target}, environment=agency.environment, is_active=True
            ).exclude(pk=self.pk).select_related("ersp_agency").first()
            if taken is not None:
                errors["ersp_agency"] = (
                    f"У «{target}» в среде «{agency.get_environment_display()}» уже есть "
                    f"действующая связь - {taken.ersp_agency.ersp_id}. Сначала снимите её."
                )

        if errors:
            raise ValidationError(errors)

    def _activated(self):
        """Связь включают снова (была снята): запись ЕРСП должна быть живой."""
        before = type(self).objects.filter(pk=self.pk).values_list("is_active", flat=True).first()
        return before is False


def _live_agency_id(links):
    link = links.filter(is_active=True).select_related("ersp_agency").first()
    if link is None or not link.ersp_agency.can_be_linked():
        return None
    return link.ersp_agency.ersp_id


def agency_id_for_department(department, environment):
    """
    agencyId, с которым уходит продажа сотрудника этого подразделения
    в среду environment: у отдела - связь его головного офиса или филиала.
    None - связи нет, или запись ЕРСП удалена или пропала из ЕРСП.
    Для модуля договоров: тогда договор не отправляется с понятной причиной.
    """
    place = department.parent if department.kind == Department.Kind.DIVISION else department
    return _live_agency_id(ErspLink.objects.filter(department=place, environment=environment))


def agency_id_for_agent(agent, environment):
    """agencyId продажи агента в среде environment (None - связи нет или запись ЕРСП не живая)."""
    return _live_agency_id(ErspLink.objects.filter(agent=agent, environment=environment))


# --- Видимость агентов (часть B4, решение 25) ----------------------------------------


def agents_placed_in(department_ids, day=None):
    """
    Условие видимости «своё подразделение» для агентов: агент, у которого
    место в одном из этих подразделений действует на дату или начнётся позже.

    Будущее место - чтобы сотрудник, прикрепивший агента с завтрашнего дня
    (или переводящий к себе), видел его сразу, а не получал «не найден».
    До даты перевода агента видят и прежнее подразделение, и новое.
    Агент, у которого место закончилось и новое не открыто, сюда не попадает:
    он виден только при видимости «вся страховая».
    """
    day = day or timezone.localdate()
    places = Placement.objects.filter(department_id__in=department_ids).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gte=day)
    )
    return Q(pk__in=places.values("agent_id"))


def visible_agents(scope, queryset=None):
    """Агенты, которых видит пользователь (scope - accounts.visibility.scope_for)."""
    queryset = Agent.objects.all() if queryset is None else queryset
    return scope.filter(queryset, department=agents_placed_in)


def journal_hidden(scope):
    """
    Строки журнала, которые человек не должен видеть: изменения агентов,
    которых он не видит (сам агент, его соглашения, места, связь с ЕРСП,
    пользователи агента и их учётные записи). None - скрывать нечего.
    """
    from django.contrib.contenttypes.models import ContentType

    if scope.kind not in (Kind.DEPARTMENTS, Kind.OWN):
        return None
    agent_type = ContentType.objects.get_for_model(Agent)
    user_type = ContentType.objects.get_for_model(User)
    hidden = Agent.objects.filter(insurance_company_id=scope.company_id).exclude(
        pk__in=visible_agents(scope).values("pk")
    ).values("pk")
    hidden_users = Member.objects.filter(agent_id__in=hidden).values("user_id")
    return (
        Q(content_type=agent_type, object_id__in=hidden)
        | Q(root_content_type=agent_type, root_object_id__in=hidden)
        | Q(content_type=user_type, object_id__in=hidden_users)
    )
