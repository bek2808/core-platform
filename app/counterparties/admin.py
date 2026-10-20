"""
Справочник контрагентов в админ-панели.

Банки, основания полномочий, должности и карточки контрагентов (с данными
юрлица внутри карточки). Удаление запрещено всем, даже суперпользователю
(см. audit.admin.TrackedAdmin): вместо него - архив или «не действует».

Мультиарендность: сотрудник видит и заводит контрагентов только своей
страховой компании; страховая подставляется из его учётной записи.
Пользователь без страховой компании (администратор системы) видит всех
и выбирает страховую при создании.
"""
from django.contrib import admin
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.forms.models import BaseInlineFormSet

from audit.admin import TrackedAdmin

from .models import (
    Bank,
    BankAccount,
    Counterparty,
    LegalEntity,
    PowerBasis,
    Position,
    Representative,
)
from .normalize import remove_spaces, search_key


@admin.register(Bank)
class BankAdmin(TrackedAdmin):
    list_display = ["mfo", "name", "inn", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["mfo", "name", "inn"]
    fields = ["mfo", "name", "inn", "is_active"]


class InputChoiceAdmin(TrackedAdmin):
    list_display = ["name", "is_other", "sort_order", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name"]
    fields = ["name", "is_other", "sort_order", "is_active"]
    readonly_fields = ["is_other"]  # «Иное» заводится только миграцией


@admin.register(PowerBasis)
class PowerBasisAdmin(InputChoiceAdmin):
    pass


@admin.register(Position)
class PositionAdmin(InputChoiceAdmin):
    pass


# --- Контрагенты ------------------------------------------------------------------

class LegalEntityFormSet(BaseInlineFormSet):
    def _construct_form(self, i, **kwargs):
        # Передаём блоку карточку (возможно, ещё не сохранённую) - см.
        # LegalEntity.card(). Без этого проверки повтора ИНН и телефона
        # в форме не срабатывали, и повтор ИНН давал ошибку 500.
        form = super()._construct_form(i, **kwargs)
        form.instance.card_in_form = self.instance
        return form


class CardChildFormSet(BaseInlineFormSet):
    """
    Проверки по всем строкам вложенного блока сразу: «основной - только один»
    и «номер не повторяется». В базе эти правила есть, но в них участвует
    ссылка на карточку, которой нет в форме, - Django их в форме не проверяет
    (ловушка из AGENTS.md), и без этой проверки была бы ошибка 500.
    """

    def clean(self):
        # Наши проверки - раньше встроенных Django: у встроенной проверки
        # повтора сообщение с техническим именем поля («number»).
        model = self.model
        rows = [
            form.instance
            for form in self.forms
            if getattr(form, "cleaned_data", None) and not form.cleaned_data.get("DELETE")
        ]
        if sum(1 for row in rows if row.is_primary) > 1:
            raise ValidationError(f"Основным может быть только один: {model._meta.verbose_name_plural}.")
        numbers = [row.number for row in rows if getattr(row, "number", "")]
        repeated = sorted({n for n in numbers if numbers.count(n) > 1})
        if repeated:
            raise ValidationError(f"Номер указан дважды: {', '.join(repeated)}.")
        super().clean()


class CardChildForm(forms.ModelForm):
    """
    Строка вложенного блока. Правило «основной - только один» проверяется не
    построчно, а по всему блоку (CardChildFormSet): построчная проверка смотрит
    в базу, где старый основной ещё основной, и не даёт сменить основного
    в одной форме. При сохранении сначала снимается старый основной
    (audit.admin.TrackedAdmin.save_formset), так что база правило не нарушит.
    """

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        exclude.add("is_primary")
        return exclude


class LegalEntityInline(admin.StackedInline):
    """Данные юрлица - блок внутри карточки контрагента."""

    model = LegalEntity
    formset = LegalEntityFormSet
    can_delete = False
    min_num = 1
    max_num = 1
    extra = 0
    verbose_name = "данные юрлица"
    verbose_name_plural = "данные юрлица"
    # ОКЭД (696 кодов) и валюты (164) - поиском, а не длинным списком.
    autocomplete_fields = ["oked", "charter_capital_currency"]
    fieldsets = [
        (None, {"fields": ["is_foreign", "country", "inn", "full_name", "short_name"]}),
        ("Деятельность", {"fields": ["oked", "okpo", "ownership_form"]}),
        ("Регистрация", {"fields": [
            "reg_certificate_number", "reg_certificate_date",
            "charter_capital", "charter_capital_currency",
        ]}),
        ("Район по СОАТО", {"fields": ["soato_code", "soato_name"]}),
    ]


class RepresentativeInline(admin.StackedInline):
    """Представители организации - блок внутри карточки."""

    model = Representative
    form = CardChildForm
    formset = CardChildFormSet
    can_delete = False  # не удаляются: срок полномочий закрывается датой «действует по»
    extra = 0
    fieldsets = [
        (None, {"fields": [
            ("full_name", "is_primary"),
            ("position", "position_other"),
            ("power_basis", "power_basis_other", "power_basis_number"),
            ("pinfl", "passport_series", "passport_number"),
            ("valid_from", "valid_to"),
        ]}),
    ]


class BankAccountInline(admin.TabularInline):
    """Банковские счета - блок внутри карточки."""

    model = BankAccount
    form = CardChildForm
    formset = CardChildFormSet
    can_delete = False  # не удаляются: ставится отметка «закрыт»
    extra = 0
    fields = ["bank", "foreign_bank_name", "number", "currency", "is_primary", "is_closed"]
    autocomplete_fields = ["bank", "currency"]


@admin.register(Counterparty)
class CounterpartyAdmin(TrackedAdmin):
    inlines = [LegalEntityInline, RepresentativeInline, BankAccountInline]
    list_display = ["display_name", "inn", "is_foreign", "region", "status"]
    list_filter = ["status", "legal_entity__is_foreign", "region"]
    list_select_related = ["legal_entity", "region", "insurance_company"]
    # Поиск устроен по-своему (get_search_results): наименование - по ключу
    # для поиска, ИНН - точно. Строка ниже нужна, чтобы появилось поле поиска.
    search_fields = ["search_text"]
    search_help_text = "Наименование (любой частью, кириллицей или латиницей) или ИНН."

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        company_id = request.user.insurance_company_id
        if company_id:
            return qs.filter(insurance_company_id=company_id)
        if request.user.is_superuser:
            return qs  # администратор системы видит все страховые
        return qs.none()

    def get_fields(self, request, obj=None):
        fields = ["phone", "email", "region", "address", "status"]
        if obj is not None:
            fields = ["display_name", *fields]
        if not request.user.insurance_company_id:
            fields = ["insurance_company", *fields]
        return fields

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            # Страховая задаётся один раз; наименование собирает система.
            readonly += ["insurance_company", "display_name"]
        return readonly

    def get_list_display(self, request):
        if request.user.insurance_company_id:
            return self.list_display
        return [*self.list_display, "insurance_company"]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        company_id = request.user.insurance_company_id
        if obj is None and company_id:
            # Новая карточка сразу получает страховую сотрудника - ещё до
            # проверки формы, чтобы проверка повтора ИНН знала, где искать.
            class FormWithCompany(form):
                def __init__(self, *args, **kw):
                    super().__init__(*args, **kw)
                    self.instance.insurance_company_id = company_id

            return FormWithCompany
        return form

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Регион по СОАТО - после сохранения всей карточки: и когда изменили
        # СОАТО, и когда сотрудник очистил регион, не трогая данные юрлица.
        form.instance.fill_region_from_soato(by=request.user)

    def get_search_results(self, request, queryset, search_term):
        term = search_term.strip()
        if not term:
            return queryset, False
        found = queryset.filter(
            Q(search_text__icontains=search_key(term))
            | Q(legal_entity__inn=remove_spaces(term).upper())
        )
        return found, False

    @admin.display(description="ИНН", ordering="legal_entity__inn")
    def inn(self, obj):
        legal_entity = getattr(obj, "legal_entity", None)
        return legal_entity.inn if legal_entity else "—"

    @admin.display(description="иностранная", boolean=True, ordering="legal_entity__is_foreign")
    def is_foreign(self, obj):
        legal_entity = getattr(obj, "legal_entity", None)
        return legal_entity.is_foreign if legal_entity else None
