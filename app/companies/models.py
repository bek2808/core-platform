"""
Страховые компании, которые работают в Core-системе (мультиарендность).

К страховой компании привязаны её сотрудники и её контрагенты; клиентов одной
страховой другая не видит. Сейчас в системе одна компания, но поле заложено
с самого начала, чтобы вторая компания не потребовала переделки.

Запись не удаляется (на неё ссылаются сотрудники, контрагенты, полисы);
если компания перестала работать - снимается отметка «работает».
"""
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

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
