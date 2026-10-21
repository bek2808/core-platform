"""
Справочники ЕРСП и перечень банков ЦБ в админ-панели: только просмотр.

Добавлять, править и удалять записи здесь нельзя никому, даже суперпользователю:
справочник - копия ЕРСП (ЦБ) и меняется только командой load_ersp_references
(load_cbu_banks).
"""
import json

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    CbuBank,
    ErspAreaType,
    ErspCountry,
    ErspCurrency,
    ErspDistrict,
    ErspInsuranceType,
    ErspInsurer,
    ErspOked,
    ErspOwnershipForm,
    ErspRegion,
    ErspResidentType,
)


class ErspReferenceAdmin(admin.ModelAdmin):
    """Общие настройки: только чтение, ответ ЕРСП показывается целиком."""

    list_filter = ["is_present"]
    readonly_fields = ["raw_pretty"]
    exclude = ["raw"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="ответ ЕРСП")
    def raw_pretty(self, obj):
        text = json.dumps(obj.raw, ensure_ascii=False, indent=2)
        return format_html("<pre>{}</pre>", text)


@admin.register(ErspRegion)
class ErspRegionAdmin(ErspReferenceAdmin):
    list_display = ["id", "name", "name_uz", "soato_code", "is_present"]
    search_fields = ["name", "name_uz", "name_uzc", "soato_code"]


@admin.register(ErspOwnershipForm)
class ErspOwnershipFormAdmin(ErspReferenceAdmin):
    list_display = ["id", "name", "is_present"]
    search_fields = ["name"]


@admin.register(ErspInsurer)
class ErspInsurerAdmin(ErspReferenceAdmin):
    list_display = ["id", "name", "tin", "industry", "license_stopped_date", "is_present"]
    list_filter = ["is_present", "industry"]
    search_fields = ["name", "tin"]


@admin.register(ErspDistrict)
class ErspDistrictAdmin(ErspReferenceAdmin):
    list_display = ["id", "name", "region", "is_present"]
    list_filter = ["is_present", "region"]
    search_fields = ["name"]


@admin.register(ErspCurrency)
class ErspCurrencyAdmin(ErspReferenceAdmin):
    list_display = ["id", "ticker", "name", "is_present"]
    search_fields = ["id", "ticker", "name", "name_uz"]


@admin.register(ErspOked)
class ErspOkedAdmin(ErspReferenceAdmin):
    list_display = ["id", "name_uz", "name", "is_present"]
    search_fields = ["id", "name", "name_uz", "name_uzc"]


class ErspNamedReferenceAdmin(ErspReferenceAdmin):
    list_display = ["id", "name", "name_uz", "is_present"]
    search_fields = ["name", "name_uz", "name_uzc"]


@admin.register(ErspAreaType)
class ErspAreaTypeAdmin(ErspNamedReferenceAdmin):
    pass


@admin.register(ErspInsuranceType)
class ErspInsuranceTypeAdmin(ErspNamedReferenceAdmin):
    pass


@admin.register(ErspResidentType)
class ErspResidentTypeAdmin(ErspNamedReferenceAdmin):
    pass


@admin.register(ErspCountry)
class ErspCountryAdmin(ErspNamedReferenceAdmin):
    pass


@admin.register(CbuBank)
class CbuBankAdmin(ErspReferenceAdmin):
    list_display = ["mfo", "branch_name", "bank_name", "inn", "kind", "changed_on", "is_present"]
    list_filter = ["is_present", "kind", "bank_name"]
    search_fields = ["mfo", "branch_name", "bank_name", "inn"]

    @admin.display(description="строка файла ЦБ")
    def raw_pretty(self, obj):
        return super().raw_pretty(obj)
