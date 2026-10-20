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
с историей; они появятся в следующей части (A2.2).
"""
import re

from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from audit.models import TrackedModel, TrackedQuerySet

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
