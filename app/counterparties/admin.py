"""
Справочник контрагентов в админ-панели.

Сейчас: банки, основания полномочий, должности. Удаление запрещено всем,
даже суперпользователю (см. audit.admin.TrackedAdmin): вместо него
снимается отметка «действует».
"""
from django.contrib import admin

from audit.admin import TrackedAdmin

from .models import Bank, PowerBasis, Position


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
