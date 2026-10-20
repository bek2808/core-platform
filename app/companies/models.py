"""
Страховые компании, которые работают в Core-системе (мультиарендность).

К страховой компании привязаны её сотрудники и её контрагенты; клиентов одной
страховой другая не видит. Сейчас в системе одна компания, но поле заложено
с самого начала, чтобы вторая компания не потребовала переделки.

Запись не удаляется (на неё ссылаются сотрудники, контрагенты, полисы);
если компания перестала работать - снимается отметка «работает».

Подразделения страховой (Department): головной офис (есть всегда, создаётся
вместе со страховой), филиалы и отделы. Модель: docs/users-model.md.
"""
import re

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from audit.models import TrackedModel
from references.models import ErspInsurer, ErspRegion

UZBEKISTAN = "UZ"


class InsuranceCompany(models.Model):
    name = models.CharField("название", max_length=255)
    code = models.SlugField(
        "краткий код",
        max_length=32,
        unique=True,
        help_text="Латиница, цифры и дефис, например: mosaic. Не меняется после создания.",
    )
    country = models.CharField(
        "страна",
        max_length=2,
        default=UZBEKISTAN,
        validators=[
            RegexValidator(r"^[A-Z]{2}$", "Код страны - две заглавные латинские буквы (ISO 3166-1), например UZ.")
        ],
        help_text="Код страны по ISO 3166-1, например UZ.",
    )
    ersp_insurer = models.OneToOneField(
        ErspInsurer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="company",
        verbose_name="страховщик в ЕРСП",
        help_text="Запись этой компании в справочнике страховщиков ЕРСП. Для Узбекистана обязательна.",
    )
    ersp_region = models.ForeignKey(
        ErspRegion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="регион ЕРСП",
        help_text=(
            "Регион компании. Уходит в ЕРСП как regionId для иностранных "
            "контрагентов. Для Узбекистана обязателен."
        ),
    )
    is_active = models.BooleanField("работает", default=True)
    created_at = models.DateTimeField("создана", auto_now_add=True)
    updated_at = models.DateTimeField("изменена", auto_now=True)

    class Meta:
        verbose_name = "страховая компания"
        verbose_name_plural = "страховые компании"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Головной офис есть у каждой страховой всегда: создаётся вместе с ней.
        adding = self._state.adding
        with transaction.atomic():
            super().save(*args, **kwargs)
            if adding:
                Department.head_office_for(self)

    def clean(self):
        # Компания из Узбекистана работает с ЕРСП, поэтому без записи
        # в справочнике страховщиков и без региона ей нельзя.
        if self.country == UZBEKISTAN:
            errors = {}
            if self.ersp_insurer_id is None:
                errors["ersp_insurer"] = "Для компании из Узбекистана укажите её запись в ЕРСП."
            if self.ersp_region_id is None:
                errors["ersp_region"] = "Для компании из Узбекистана укажите регион."
            if errors:
                raise ValidationError(errors)


# --- Подразделения ----------------------------------------------------------------

DEPARTMENT_CODE_REGEX = r"^[0-9]{2,6}$"
HEAD_OFFICE_NAME = "Головной офис"
_SPACES = re.compile(r"\s+")


class Department(TrackedModel):
    """
    Подразделение страховой: головной офис, филиал или отдел.

    Не больше двух уровней: отдел входит в головной офис или филиал, внутри
    отдела отделов нет. Головной офис один на страховую и создаётся системой
    вместе со страховой; филиалы и отделы заводит администратор страховой.

    Страховая, вид и «входит в» после создания не меняются (проверяет
    и база - триггер в миграции companies/0002): иначе история прикреплений
    людей перестала бы показывать, где человек был на дату договора.
    Реорганизация - закрыть старое подразделение и завести новое.

    Подразделение не удаляют, а закрывают (на него ссылаются люди и договоры).
    """

    class Kind(models.TextChoices):
        HEAD_OFFICE = "head_office", "головной офис"
        BRANCH = "branch", "филиал"
        DIVISION = "division", "отдел"

    class Status(models.TextChoices):
        ACTIVE = "active", "действует"
        CLOSED = "closed", "закрыто"

    insurance_company = models.ForeignKey(
        InsuranceCompany,
        on_delete=models.PROTECT,
        related_name="departments",
        verbose_name="страховая компания",
    )
    kind = models.CharField("вид", max_length=16, choices=Kind.choices)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="входит в",
        help_text="Только для отдела: головной офис или филиал, в который он входит.",
    )
    name = models.CharField("название", max_length=255)
    code = models.CharField(
        "код",
        max_length=6,
        blank=True,
        validators=[RegexValidator(DEPARTMENT_CODE_REGEX, "Код - от 2 до 6 цифр, например 0101.")],
        help_text=(
            "Необязателен. От 2 до 6 цифр; входит в номер договора по правилу "
            "страховой. Не повторяется внутри страховой."
        ),
    )
    region = models.ForeignKey(
        ErspRegion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="регион",
        help_text=(
            "Только для филиала, обязателен. Головной офис берёт регион "
            "страховой, отдел - регион своего головного офиса или филиала."
        ),
    )
    status = models.CharField(
        "статус", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )

    class Meta:
        verbose_name = "подразделение"
        verbose_name_plural = "подразделения"
        ordering = ["insurance_company__name", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["insurance_company"],
                condition=Q(kind="head_office"),
                name="companies_department_one_head_office",
                violation_error_message="У страховой уже есть головной офис.",
            ),
            models.UniqueConstraint(
                fields=["insurance_company", "code"],
                condition=~Q(code=""),
                name="companies_department_code_unique",
                violation_error_message="Такой код уже есть у другого подразделения страховой.",
            ),
            # Два действующих подразделения с одним названием в одном месте
            # спутаются в списках. «Входит в» пусто у головного офиса
            # и филиалов - для правила пусто считается одинаковым значением.
            models.UniqueConstraint(
                Lower("name"), "insurance_company", "parent",
                condition=Q(status="active"),
                nulls_distinct=False,
                name="companies_department_name_unique",
                violation_error_message="Действующее подразделение с таким названием здесь уже есть.",
            ),
            models.CheckConstraint(
                condition=Q(code="") | Q(code__regex=DEPARTMENT_CODE_REGEX),
                name="companies_department_code_digits",
                violation_error_message="Код - от 2 до 6 цифр.",
            ),
            models.CheckConstraint(
                condition=(Q(kind="division") & Q(parent__isnull=False))
                | (~Q(kind="division") & Q(parent__isnull=True)),
                name="companies_department_parent_by_kind",
                violation_error_message="Отдел входит в головной офис или филиал; у них самих «входит в» пусто.",
            ),
            models.CheckConstraint(
                condition=(Q(kind="branch") & Q(region__isnull=False))
                | (~Q(kind="branch") & Q(region__isnull=True)),
                name="companies_department_region_by_kind",
                violation_error_message="Регион задаётся только у филиала, и у филиала он обязателен.",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["head_office", "branch", "division"]),
                name="companies_department_kind_known",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["active", "closed"]),
                name="companies_department_status_known",
            ),
            models.CheckConstraint(
                condition=~Q(kind="head_office") | Q(status="active"),
                name="companies_department_head_office_active",
                violation_error_message="Головной офис не закрывается.",
            ),
        ]

    def __str__(self):
        return self.name

    def full_name(self):
        """«Филиал Самарканд › Продажи»: название вместе с тем, во что входит."""
        if self.parent_id is None:
            return self.name
        return f"{self.parent.name} › {self.name}"

    full_name.short_description = "подразделение"

    def effective_region(self):
        """Регион подразделения: у филиала - свой, у головного офиса - страховой, у отдела - родителя."""
        if self.kind == self.Kind.BRANCH:
            return self.region
        if self.kind == self.Kind.DIVISION and self.parent is not None:
            return self.parent.effective_region()
        return self.insurance_company.ersp_region

    effective_region.short_description = "регион"

    @classmethod
    def head_office_for(cls, company, by=None):
        """Головной офис страховой; если его ещё нет - создать (от имени системы, если by не задан)."""
        office = cls.objects.filter(insurance_company=company, kind=cls.Kind.HEAD_OFFICE).first()
        if office is None:
            office = cls(insurance_company=company, kind=cls.Kind.HEAD_OFFICE, name=HEAD_OFFICE_NAME)
            office.save(by=by)
        return office

    def normalize(self):
        self.name = _SPACES.sub(" ", self.name or "").strip()
        self.code = _SPACES.sub("", self.code or "")

    def clean(self):
        # Правила здесь повторяют проверки базы: часть полей (страховая,
        # вид при правке) в форме нет, и без этого сотрудник получил бы
        # ошибку 500 из базы вместо понятного сообщения (ловушка из AGENTS.md).
        self.normalize()
        errors = {}
        company_id = self.insurance_company_id
        parent = self.parent if self.parent_id else None

        if self.pk is not None:
            before = (
                type(self).objects.filter(pk=self.pk)
                .values("insurance_company_id", "kind", "parent_id").first()
            )
            if before is not None:
                if before["kind"] != self.kind:
                    errors["kind"] = "Вид подразделения не меняется после создания."
                if before["parent_id"] != self.parent_id:
                    errors["parent"] = (
                        "«Входит в» не меняется после создания. Для реорганизации "
                        "закройте подразделение и заведите новое."
                    )
                if before["insurance_company_id"] != company_id:
                    errors["insurance_company"] = "Страховая подразделения не меняется."

        if self.kind == self.Kind.DIVISION:
            if parent is None:
                errors.setdefault("parent", "Укажите, в какой головной офис или филиал входит отдел.")
            elif parent.kind == self.Kind.DIVISION:
                errors.setdefault("parent", "Отдел не может входить в другой отдел - только в головной офис или филиал.")
            elif company_id is not None and parent.insurance_company_id != company_id:
                errors.setdefault("parent", "Это подразделение другой страховой.")
            elif parent.status == self.Status.CLOSED and self.status == self.Status.ACTIVE:
                errors.setdefault("parent", f"«{parent.name}» закрыто - в него нельзя добавить отдел.")
        elif parent is not None:
            errors.setdefault("parent", "У головного офиса и филиала поле «входит в» пустое.")

        if self.kind == self.Kind.BRANCH and self.region_id is None:
            errors["region"] = "Укажите регион филиала."
        elif self.kind != self.Kind.BRANCH and self.region_id is not None:
            errors["region"] = "Регион задаётся только у филиала."

        if self.status == self.Status.CLOSED:
            if self.kind == self.Kind.HEAD_OFFICE:
                errors["status"] = "Головной офис не закрывается."
            elif self.pk is not None:
                open_children = self.children.filter(status=self.Status.ACTIVE)
                if open_children.exists():
                    names = ", ".join(open_children.values_list("name", flat=True)[:5])
                    errors["status"] = f"Сначала закройте отделы в нём: {names}."
                else:
                    # Люди, которые работают здесь сегодня или с будущей даты.
                    today = timezone.localdate()
                    working = self.assignments.filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today))
                    if working.exists():
                        names = ", ".join(a.user.full_name() for a in working.select_related("user")[:5])
                        errors["status"] = (
                            f"Здесь работают люди: {names}. Сначала переведите их или закройте "
                            "их прикрепления датой «по»."
                        )

        if company_id is not None:
            others = type(self).objects.filter(insurance_company_id=company_id).exclude(pk=self.pk)
            if self.code and others.filter(code=self.code).exists():
                taken = others.filter(code=self.code).first()
                errors["code"] = f"Код {self.code} уже у подразделения «{taken.name}»."
            if self.status == self.Status.ACTIVE and self.name and others.filter(
                status=self.Status.ACTIVE, parent_id=self.parent_id, name__iexact=self.name
            ).exists():
                errors["name"] = "Действующее подразделение с таким названием здесь уже есть."

        if errors:
            raise ValidationError(errors)
