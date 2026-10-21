"""
Агенты страховой и связь наших записей с ЕРСП (модель: docs/users-model.md,
раздел «Агенты (этап B)»).

Сейчас (часть B1) здесь только связь с ЕРСП (ErspLink): какой agencyId
у головного офиса и филиалов страховой. Агенты, соглашения, места агентов
и пользователи агентов появятся в частях B2-B3, и связь агента с ЕРСП -
в этой же таблице.

agencyId уходит в ЕРСП в каждом договоре, полисе, подтверждении оплаты:
продал сотрудник - agencyId его подразделения (у отдела - его головного
офиса или филиала: отделов в ЕРСП нет), продал агент - agencyId агента.
В песочнице и в боевой среде id разные, поэтому связь - своя в каждой
среде (среда берётся из записи справочника ЕРСП).
"""
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from audit.models import TrackedModel
from companies.models import Department
from integrations.models import Environment, ErspAgency


class ErspLink(TrackedModel):
    """
    Связь нашей записи с записью справочника ЕРСП «Агенты/филиалы».

    Правила (в базе - ограничения и триггер agents_ersplink_rules, они же
    в clean(): у формы нет страховой и среды, без этого была бы ошибка 500):
    - одна запись ЕРСП - одна наша запись, навсегда (связь не переносится:
      по ней видно, с каким agencyId уходили договоры);
    - у подразделения в каждой среде - не больше одной действующей связи;
    - связывается только головной офис или филиал (отдел берёт связь
      своего родителя) своей страховой, с записью ЕРСП своей страховой;
    - новую связь нельзя сделать с записью, удалённой в ЕРСП или пропавшей
      из него; запись ЕРСП, подразделение, страховая и среда после
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
    is_active = models.BooleanField(
        "действует", default=True,
        help_text="Снимите, если запись в ЕРСП удалили или заменили: связь останется в истории.",
    )

    class Meta:
        verbose_name = "связь с ЕРСП"
        verbose_name_plural = "связи с ЕРСП"
        ordering = ["environment", "ersp_agency__ersp_id"]
        constraints = [
            # Часть B2 добавит агента: тогда «ровно одно из двух».
            models.CheckConstraint(
                condition=Q(department__isnull=False),
                name="agents_ersplink_has_target",
                violation_error_message="Укажите, с чем связана запись ЕРСП.",
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
        ]

    def __str__(self):
        agency = self.ersp_agency if self.ersp_agency_id else None
        return f"{agency.ersp_id if agency else '?'} ({self.get_environment_display()})"

    def target(self):
        """С чем связана запись ЕРСП."""
        return self.department

    def journal_root(self):
        # Связь видна в «Истории» подразделения.
        return self.department

    def normalize(self):
        # Страховая и среда - всегда от записи ЕРСП: их нет в форме.
        if self.ersp_agency_id:
            self.insurance_company_id = self.ersp_agency.insurance_company_id
            self.environment = self.ersp_agency.environment

    def clean(self):
        self.normalize()
        errors = {}
        agency = self.ersp_agency if self.ersp_agency_id else None
        department = self.department if self.department_id else None

        if self.pk is not None:
            before = type(self).objects.filter(pk=self.pk).values("ersp_agency_id", "department_id").first()
            if before is not None and (
                before["ersp_agency_id"] != self.ersp_agency_id or before["department_id"] != self.department_id
            ):
                errors["ersp_agency"] = (
                    "Связь не переносится. Снимите отметку «действует» и заведите новую связь."
                )

        if department is not None:
            if department.kind == Department.Kind.DIVISION:
                errors["department"] = (
                    "Отдел с ЕРСП не связывается: в ЕРСП отделов нет, он берёт связь своего "
                    "головного офиса или филиала."
                )
            elif agency is not None and department.insurance_company_id != agency.insurance_company_id:
                errors["ersp_agency"] = "Это запись ЕРСП другой страховой."

        if agency is not None and self.is_active and (self.pk is None or self._activated()):
            if agency.is_deleted:
                errors["ersp_agency"] = f"Запись {agency.ersp_id} удалена в ЕРСП - её agencyId больше не принимается."
            elif not agency.is_present:
                errors["ersp_agency"] = (
                    f"Записи {agency.ersp_id} нет в последней загрузке справочника ЕРСП. "
                    "Загрузите справочник заново (load_ersp_agencies)."
                )

        if department is not None and agency is not None and self.is_active and "department" not in errors:
            taken = type(self).objects.filter(
                department=department, environment=agency.environment, is_active=True
            ).exclude(pk=self.pk).select_related("ersp_agency").first()
            if taken is not None:
                errors["ersp_agency"] = (
                    f"У «{department}» в среде «{agency.get_environment_display()}» уже есть "
                    f"действующая связь - {taken.ersp_agency.ersp_id}. Сначала снимите её."
                )

        if errors:
            raise ValidationError(errors)

    def _activated(self):
        """Связь включают снова (была снята): запись ЕРСП должна быть живой."""
        before = type(self).objects.filter(pk=self.pk).values_list("is_active", flat=True).first()
        return before is False


def agency_id_for_department(department, environment):
    """
    agencyId, с которым уходит продажа сотрудника этого подразделения
    в среду environment: у отдела - связь его головного офиса или филиала.
    None - связи нет, или запись ЕРСП удалена или пропала из ЕРСП.
    Для модуля договоров: тогда договор не отправляется с понятной причиной.
    """
    place = department.parent if department.kind == Department.Kind.DIVISION else department
    link = (
        ErspLink.objects.filter(department=place, environment=environment, is_active=True)
        .select_related("ersp_agency").first()
    )
    if link is None or not link.ersp_agency.can_be_linked():
        return None
    return link.ersp_agency.ersp_id
