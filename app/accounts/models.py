"""
Модель пользователя Core-системы (модель данных: docs/users-model.md).

Стандартная модель Django (логин, имя, фамилия, e-mail, пароль, «активен»)
плюс: отчество, вид (штатный сотрудник, работник по ГПХ, агент, платформа),
телефон, ПИНФЛ и страховая компания.

Пользователь - таблица с журналом изменений (TrackedModel): видно, кто его
завёл, заблокировал, сменил данные. Пароль в журнал не попадает - только
факт смены («***»). Время последнего входа и пересчёт хеша пароля при входе
в журнал не пишутся (audit_service_fields): иначе каждый вход затирал бы
«кто изменил».

Пользователя не удаляют: уволенного блокируют (снимают «активен»), договоры
остаются за ним.

Где работает человек (подразделение, должность, роли) - в прикреплениях
с историей (Assignment): с какой даты по какую. На любую дату у человека
не больше одного прикрепления - это проверяет база (ограничение исключения
на диапазон дат, расширение btree_gist).

Должности (JobPosition) - общий справочник для всех страховых.
Роли (Role) - каталог платформы: роль = группа прав Django + описание;
заводятся миграциями, страховая их только назначает.

Права - роли прикрепления, действующего сегодня (accounts/backends.py);
группы в учётной записи - только у администратора платформы. «Статус
персонала» (вход в админ-панель) выставляет система: у всех, кроме агентов.
"""
import re

from django.contrib.auth.models import AbstractUser, Group
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeBoundary, RangeOperators
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F, Func, Q
from django.db.models.functions import Lower
from django.utils import timezone

from audit.models import TrackedModel, TrackedQuerySet, write_journal_value

PHONE_REGEX = r"^[0-9]{7,15}$"      # международный формат: только цифры, как у контрагентов
PINFL_REGEX = r"^[0-9]{14}$"
_SPACES = re.compile(r"\s+")
_NOT_DIGITS = re.compile(r"[^0-9]")


class UserManager(DjangoUserManager.from_queryset(TrackedQuerySet)):
    """Обычный менеджер пользователей Django, но без массовых операций в обход журнала."""


class User(AbstractUser, TrackedModel):
    class Kind(models.TextChoices):
        EMPLOYEE = "employee", "штатный сотрудник"
        CONTRACTOR = "contractor", "работник по ГПХ"
        AGENT = "agent", "агент"
        PLATFORM = "platform", "администратор платформы"

    insurance_company = models.ForeignKey(
        "companies.InsuranceCompany",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="users",
        verbose_name="страховая компания",
        help_text=(
            "Компания, в которой работает пользователь: он видит только её данные. "
            "Пусто - у администратора платформы и аварийной технической записи. "
            "После создания не меняется."
        ),
    )
    patronymic = models.CharField("отчество", max_length=150, blank=True)
    kind = models.CharField(
        "вид", max_length=16, choices=Kind.choices, default=Kind.EMPLOYEE,
        help_text="Штатный сотрудник, работник по ГПХ, агент или администратор платформы.",
    )
    phone = models.CharField(
        "телефон",
        max_length=15,
        blank=True,
        validators=[RegexValidator(PHONE_REGEX, "Телефон - от 7 до 15 цифр с кодом страны, например 998901234567.")],
        help_text="С кодом страны, без «+». Пробелы, скобки и дефисы уберутся сами. Понадобится для входа по SMS.",
    )
    pinfl = models.CharField(
        "ПИНФЛ",
        max_length=14,
        blank=True,
        validators=[RegexValidator(PINFL_REGEX, "ПИНФЛ - 14 цифр.")],
        help_text="14 цифр. Понадобится для входа через OneID.",
    )
    must_change_password = models.BooleanField(
        "сменить пароль при входе",
        default=False,
        help_text=(
            "Ставит система, когда выдаёт временный пароль (новый пользователь, «Сбросить пароль»). "
            "Пока человек не задаст свой пароль, другие страницы не открываются."
        ),
    )

    objects = UserManager()

    # Время последнего входа и пересчёт хеша пароля - служебные сохранения.
    audit_service_fields = ("last_login", "password")
    audit_exclude = ("last_login",)
    audit_mask = ("password",)

    class Meta:
        verbose_name = "пользователь"
        verbose_name_plural = "пользователи"
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=["employee", "contractor", "agent", "platform"]),
                name="accounts_user_kind_known",
            ),
            models.CheckConstraint(
                condition=~Q(kind="platform") | Q(insurance_company__isnull=True),
                name="accounts_user_platform_no_company",
                violation_error_message="Администратор платформы не принадлежит страховой компании.",
            ),
            models.CheckConstraint(
                condition=Q(phone="") | Q(phone__regex=PHONE_REGEX),
                name="accounts_user_phone_digits",
                violation_error_message="Телефон - от 7 до 15 цифр.",
            ),
            models.CheckConstraint(
                condition=Q(pinfl="") | Q(pinfl__regex=PINFL_REGEX),
                name="accounts_user_pinfl_digits",
                violation_error_message="ПИНФЛ - 14 цифр.",
            ),
        ]

    def normalize(self):
        # «Статус персонала» (вход в админ-панель) выставляет система: у агентов
        # будет свой кабинет, остальные работают в админ-панели. Что человек
        # там видит и может - решают роли его прикрепления (accounts/backends.py).
        self.is_staff = self.kind != self.Kind.AGENT
        self.first_name = _SPACES.sub(" ", self.first_name or "").strip()
        self.last_name = _SPACES.sub(" ", self.last_name or "").strip()
        self.patronymic = _SPACES.sub(" ", self.patronymic or "").strip()
        self.phone = _NOT_DIGITS.sub("", self.phone or "")
        self.pinfl = _SPACES.sub("", self.pinfl or "")

    def full_name(self):
        """Фамилия Имя Отчество (если ничего не заполнено - логин)."""
        parts = [self.last_name, self.first_name, self.patronymic]
        return " ".join(part for part in parts if part) or self.get_username()

    full_name.short_description = "ФИО"

    def clean(self):
        # Правила повторяют проверки базы: форма не знает про часть из них,
        # и без этого сотрудник получил бы ошибку 500 вместо сообщения.
        super().clean()
        self.normalize()
        errors = {}
        if self.kind == self.Kind.PLATFORM:
            if self.insurance_company_id is not None:
                errors["insurance_company"] = "Администратор платформы не принадлежит страховой компании."
        elif self.insurance_company_id is None and not self.is_superuser:
            errors["insurance_company"] = "Укажите страховую компанию."
        if self.pk is not None:
            before = type(self).objects.filter(pk=self.pk).values_list("insurance_company_id", flat=True)
            if before and before[0] is not None and before[0] != self.insurance_company_id:
                errors["insurance_company"] = (
                    "Страховая компания не меняется после создания. "
                    "Человек перешёл в другую страховую - заведите ему новую учётную запись."
                )
        if errors:
            raise ValidationError(errors)


# --- Должности сотрудников -------------------------------------------------------


class JobPosition(TrackedModel):
    """
    Должность сотрудника страховой. Общий справочник для всех страховых
    (решение Бекзода 21.10): администратор страховой добавляет должность -
    она видна всем. Переименовать и снять может только администратор
    платформы, иначе одна страховая поменяла бы должность сотрудникам всех.

    Не путать с «Должностями» контрагентов (counterparties.Position): там
    должности представителей чужих организаций для ЕРСП.
    """

    name = models.CharField("название", max_length=255)
    sort_order = models.PositiveSmallIntegerField(
        "порядок в списке",
        default=100,
        help_text="Меньше - выше в списке. Одинаковые - по алфавиту.",
    )
    is_active = models.BooleanField(
        "действует",
        default=True,
        help_text="Снятая должность не предлагается для новых прикреплений; у прежних остаётся.",
    )

    class Meta:
        verbose_name = "должность сотрудника"
        verbose_name_plural = "должности сотрудников"
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="accounts_jobposition_name_unique",
                violation_error_message="Такая должность уже есть.",
            ),
        ]

    def __str__(self):
        return self.name

    def normalize(self):
        self.name = _SPACES.sub(" ", self.name or "").strip()
        if self.name:
            self.name = self.name[0].upper() + self.name[1:]


# --- Роли ------------------------------------------------------------------------


class Visibility(models.TextChoices):
    """Чьи данные видит человек (модель: docs/users-model.md, «Видимость данных»)."""

    OWN = "own", "свои"
    DEPARTMENT = "department", "своё подразделение"
    COMPANY = "company", "вся страховая"


class Role(models.Model):
    """
    Роль - что человек может делать. Роль = группа прав Django + описание.

    Каталог один на платформу и заводится миграциями: администратор страховой
    роли только назначает, состав прав не меняет. Людям экран ролей - только
    для чтения (в том числе суперпользователю: правка - миграцией через git).
    """

    class Audience(models.TextChoices):
        COMPANY = "company", "сотрудники страховой"
        AGENT = "agent", "агенты"
        PLATFORM = "platform", "платформа"

    group = models.OneToOneField(
        Group, on_delete=models.PROTECT, related_name="role", verbose_name="группа прав"
    )
    code = models.SlugField("код", max_length=32, unique=True, help_text="Латиница, не меняется.")
    audience = models.CharField("для кого", max_length=16, choices=Audience.choices)
    default_visibility = models.CharField(
        "видимость по умолчанию", max_length=16, choices=Visibility.choices, blank=True,
    )
    description = models.TextField("что умеет", blank=True)
    sort_order = models.PositiveSmallIntegerField("порядок в списке", default=100)

    class Meta:
        verbose_name = "роль"
        verbose_name_plural = "роли"
        ordering = ["sort_order", "code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(audience__in=["company", "agent", "platform"]),
                name="accounts_role_audience_known",
            ),
        ]

    def __str__(self):
        return self.group.name


# --- Прикрепления ----------------------------------------------------------------


class DateRange(Func):
    """daterange(с, по, '[]') в PostgreSQL: «по» включительно, пусто - без конца."""

    function = "daterange"
    output_field = DateRangeField()


def _period():
    return DateRange("valid_from", "valid_to", RangeBoundary(inclusive_lower=True, inclusive_upper=True))


class AssignmentQuerySet(TrackedQuerySet):
    def on(self, day=None):
        """Действующие на дату (по умолчанию - сегодня по часовому поясу системы)."""
        day = day or timezone.localdate()
        return self.filter(Q(valid_to__isnull=True) | Q(valid_to__gte=day), valid_from__lte=day)


class Assignment(TrackedModel):
    """
    Прикрепление: где и кем человек работает с какой даты по какую.

    На любую дату у человека не больше одного прикрепления, у подразделения -
    не больше одного руководителя: обе проверки держит база (ограничения
    исключения). Подразделение - той же страховой, что и человек (триггер).
    Владелец прикрепления не меняется. Удаления нет: прикрепление закрывают
    датой «по».

    Роли - связь «многие ко многим» (отдельная таблица): меняются только
    методом set_roles(), который пишет смену ролей в журнал.
    """

    class AssistantScope(models.TextChoices):
        ALL = "all", "все договоры менеджера"
        ENTERED = "entered", "только введённые им"

    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="assignments", verbose_name="пользователь"
    )
    department = models.ForeignKey(
        "companies.Department",
        on_delete=models.PROTECT,
        related_name="assignments",
        verbose_name="подразделение",
    )
    position = models.ForeignKey(
        JobPosition, on_delete=models.PROTECT, related_name="assignments", verbose_name="должность"
    )
    roles = models.ManyToManyField(
        Role,
        blank=True,
        related_name="assignments",
        verbose_name="роли",
        limit_choices_to={"audience": Role.Audience.COMPANY},
        help_text="Что человек может делать. Можно несколько.",
    )
    is_head = models.BooleanField(
        "руководитель подразделения",
        default=False,
        help_text="Видит данные своего подразделения. На дату у подразделения - один руководитель.",
    )
    visibility = models.CharField(
        "видимость",
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.COMPANY,
        help_text="Чьи данные видит: свои, своего подразделения или всей страховой.",
    )
    assistant_to = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assistants",
        verbose_name="ассистент для",
        help_text="Менеджер, за которого человек вводит договоры. Пусто - не ассистент.",
    )
    assistant_scope = models.CharField(
        "ассистент видит",
        max_length=16,
        choices=AssistantScope.choices,
        blank=True,
        help_text="Только если указан менеджер.",
    )
    valid_from = models.DateField("действует с")
    valid_to = models.DateField(
        "действует по", null=True, blank=True, help_text="Включительно. Пусто - по сей день."
    )
    basis = models.CharField(
        "основание", max_length=255, blank=True, help_text="Приказ, соглашение ГПХ и т. п."
    )

    objects = AssignmentQuerySet.as_manager()

    class Meta:
        verbose_name = "прикрепление"
        verbose_name_plural = "прикрепления"
        ordering = ["user", "-valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="accounts_assignment_dates_order",
                violation_error_message="«Действует по» не может быть раньше «действует с».",
            ),
            models.CheckConstraint(
                condition=Q(visibility__in=["own", "department", "company"]),
                name="accounts_assignment_visibility_known",
            ),
            models.CheckConstraint(
                condition=~Q(is_head=True, visibility="own"),
                name="accounts_assignment_head_sees_department",
                violation_error_message="Руководитель видит не меньше своего подразделения.",
            ),
            models.CheckConstraint(
                condition=(Q(assistant_to__isnull=True) & Q(assistant_scope=""))
                | (Q(assistant_to__isnull=False) & Q(assistant_scope__in=["all", "entered"])),
                name="accounts_assignment_assistant_scope",
                violation_error_message="«Ассистент видит» заполняется вместе с менеджером, и только с ним.",
            ),
            models.CheckConstraint(
                condition=~Q(assistant_to=F("user")),
                name="accounts_assignment_not_own_assistant",
                violation_error_message="Человек не может быть ассистентом самому себе.",
            ),
            ExclusionConstraint(
                name="accounts_assignment_no_overlap",
                expressions=[("user", RangeOperators.EQUAL), (_period(), RangeOperators.OVERLAPS)],
                violation_error_message="На эти даты у человека уже есть другое прикрепление.",
            ),
            ExclusionConstraint(
                name="accounts_assignment_one_head",
                expressions=[("department", RangeOperators.EQUAL), (_period(), RangeOperators.OVERLAPS)],
                condition=Q(is_head=True),
                violation_error_message="На эти даты у подразделения уже есть руководитель.",
            ),
        ]

    def __str__(self):
        user = self._owner()
        who = user.full_name() if user is not None else "?"
        position = self.position if self.position_id else "—"
        since = f", с {self.valid_from:%d.%m.%Y}" if self.valid_from else ""
        return f"{who}: {position}{since}"

    @property
    def insurance_company_id(self):
        # Для журнала изменений: строка журнала видна только своей страховой.
        user = self._owner()
        return user.insurance_company_id if user is not None else None

    def journal_root(self):
        # Изменения прикрепления видны в «Истории» пользователя.
        return self._owner()

    def _owner(self):
        try:
            return self.user
        except User.DoesNotExist:
            return None

    def is_current(self, day=None):
        day = day or timezone.localdate()
        return self.valid_from <= day and (self.valid_to is None or self.valid_to >= day)

    def normalize(self):
        self.basis = _SPACES.sub(" ", self.basis or "").strip()
        if self.assistant_to_id is None:
            self.assistant_scope = ""

    def clean(self):
        # Правила повторяют проверки базы (ловушка из AGENTS.md: владельца
        # в форме нет, иначе - ошибка 500 вместо сообщения).
        self.normalize()
        errors = {}
        user = self._owner()
        company_id = user.insurance_company_id if user is not None else None
        department = self.department if self.department_id else None

        if user is not None and company_id is None:
            errors["department"] = "У пользователя без страховой прикреплений нет."
        if department is not None and company_id is not None:
            if department.insurance_company_id != company_id:
                errors["department"] = "Это подразделение другой страховой."
            elif department.status != department.Status.ACTIVE and self._changed("department_id"):
                errors["department"] = f"Подразделение «{department.name}» закрыто."

        if self.position_id and not self.position.is_active and self._changed("position_id"):
            errors["position"] = f"Должность «{self.position}» снята - выберите действующую."

        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            # Ошибка здесь заодно выключает проверку пересечений с базой: с такими
            # датами PostgreSQL не построит период и ответил бы ошибкой (500).
            errors["valid_to"] = "«Действует по» не может быть раньше «действует с»."

        if self.is_head and self.visibility == Visibility.OWN:
            errors["visibility"] = "Руководитель видит не меньше своего подразделения."

        if self.assistant_to_id is not None:
            manager = self.assistant_to
            if not self.assistant_scope:
                errors["assistant_scope"] = "Укажите, что видит ассистент."
            if user is not None and (manager.pk == user.pk and user.pk is not None):
                errors["assistant_to"] = "Человек не может быть ассистентом самому себе."
            elif company_id is not None and manager.insurance_company_id != company_id:
                errors["assistant_to"] = "Менеджер - из другой страховой."

        if self.pk is not None:
            before_user = type(self).objects.filter(pk=self.pk).values_list("user_id", flat=True).first()
            if before_user is not None and before_user != self.user_id:
                errors["user"] = "Прикрепление не переносится на другого человека."

        if errors:
            raise ValidationError(errors)

    def _changed(self, attname):
        """Новая запись или поле изменилось относительно базы."""
        if self.pk is None:
            return True
        before = type(self).objects.filter(pk=self.pk).values_list(attname, flat=True).first()
        return before != getattr(self, attname)

    def set_roles(self, roles, by=None, source=None):
        """Задать роли и записать смену в журнал («было -> стало» названиями ролей)."""
        roles = list(roles)
        before = sorted(str(role) for role in self.roles.all())
        after = sorted(str(role) for role in roles)
        self.roles.set(roles)
        write_journal_value(
            self, "roles", ", ".join(before) or None, ", ".join(after) or None, by=by, source=source,
        )


def current_assignment(user, day=None):
    """Действующее на дату прикрепление пользователя или None."""
    return user.assignments.on(day).select_related("department", "position").first()
