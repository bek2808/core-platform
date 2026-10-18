from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    # Стандартная форма пользователя Django + блок «Страховая компания».
    fieldsets = [
        *BaseUserAdmin.fieldsets,
        ("Страховая компания", {"fields": ["insurance_company"]}),
    ]
    add_fieldsets = [
        *BaseUserAdmin.add_fieldsets,
        ("Страховая компания", {"fields": ["insurance_company"]}),
    ]
    list_display = [*BaseUserAdmin.list_display, "insurance_company"]
    list_filter = [*BaseUserAdmin.list_filter, "insurance_company"]
