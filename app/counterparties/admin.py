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
from urllib.parse import urlencode

from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.forms.models import BaseInlineFormSet
from django.shortcuts import redirect
from django.urls import reverse

from audit.admin import TrackedAdmin

from .models import Bank, Counterparty, LegalEntity, PowerBasis, Position
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


@admin.register(Counterparty)
class CounterpartyAdmin(TrackedAdmin):
    inlines = [LegalEntityInline]
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

    def history_view(self, request, object_id, extra_context=None):
        # История карточки - вместе с данными юрлица: у них общий номер записи.
        types = ContentType.objects.get_for_models(Counterparty, LegalEntity).values()
        query = urlencode({
            "content_type__id__in": ",".join(str(t.pk) for t in types),
            "object_id": object_id,
        })
        return redirect(f"{reverse('admin:audit_changelog_changelist')}?{query}")

    @admin.display(description="ИНН", ordering="legal_entity__inn")
    def inn(self, obj):
        legal_entity = getattr(obj, "legal_entity", None)
        return legal_entity.inn if legal_entity else "—"

    @admin.display(description="иностранная", boolean=True, ordering="legal_entity__is_foreign")
    def is_foreign(self, obj):
        legal_entity = getattr(obj, "legal_entity", None)
        return legal_entity.is_foreign if legal_entity else None
