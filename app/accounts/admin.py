"""
Пользователи в админ-панели.

Стандартная форма Django + отчество, вид, телефон, ПИНФЛ и страховая компания.
Основа - audit.admin.TrackedAdmin: изменения пишутся в журнал с именем того,
кто сохранил; удаления нет (уволенного блокируют: снимают «активен»).

Мультиарендность: пользователь страховой видит и заводит только пользователей
своей страховой. Суперпользователь (аварийная техническая запись) видит всех
и выбирает страховую при создании. Страховая после создания не меняется.

Защита от повышения прав: не суперпользователь не видит и не меняет
«статус суперпользователя», группы и отдельные права. Роли и прикрепления
администратору страховой дадим вместе с его правами на пользователей (часть A3).
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from audit.admin import TrackedAdmin

from .models import User

# Что не суперпользователю менять нельзя: иначе администратор страховой
# мог бы сделать суперпользователем себя или кого-то ещё.
PRIVILEGE_FIELDS = ["is_superuser", "groups", "user_permissions"]


def own_users(request, queryset):
    """Своя страховая; суперпользователь - все; иначе - никто."""
    company_id = request.user.insurance_company_id
    if company_id:
        return queryset.filter(insurance_company_id=company_id)
    if request.user.is_superuser:
        return queryset
    return queryset.none()


@admin.register(User)
class UserAdmin(TrackedAdmin, BaseUserAdmin):
    list_display = ["username", "full_name", "kind", "insurance_company", "is_active", "is_staff"]
    list_filter = ["kind", "is_active", "insurance_company"]
    search_fields = ["username", "first_name", "last_name", "patronymic", "email", "phone", "pinfl"]
    ordering = ["username"]

    fieldsets = [
        (None, {"fields": ["username", "password"]}),
        ("Личные данные", {"fields": ["last_name", "first_name", "patronymic", "email", "phone", "pinfl"]}),
        ("Принадлежность", {"fields": ["kind", "insurance_company"]}),
        ("Права", {"fields": ["is_active", "is_staff", "is_superuser", "groups", "user_permissions"]}),
        ("Даты", {"fields": ["last_login", "date_joined"]}),
    ]
    add_fieldsets = [
        (None, {"classes": ["wide"], "fields": ["username", "usable_password", "password1", "password2"]}),
        ("Личные данные", {"fields": ["last_name", "first_name", "patronymic", "email", "phone", "pinfl"]}),
        ("Принадлежность", {"fields": ["kind", "insurance_company"]}),
    ]

    def get_queryset(self, request):
        return own_users(request, super().get_queryset(request))

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name in ("phone", "pinfl"):
            # Человек вводит «+998 (90) 123-45-67» или «3010 5780 1234 56»: лишние
            # знаки система убирает сама (User.normalize), а длину проверяет модель
            # уже по цифрам. Форма не должна резать ввод до этого.
            kwargs["max_length"] = 40
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if request.user.is_superuser:
            return fieldsets
        # Страховая у не суперпользователя - его собственная, выбирать нечего.
        return [
            (name, {**options, "fields": [f for f in options["fields"] if f != "insurance_company"]})
            for name, options in fieldsets
        ]

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        readonly += ["last_login", "date_joined"]
        if not request.user.is_superuser:
            readonly += PRIVILEGE_FIELDS
        if obj is not None:
            readonly += ["insurance_company"]  # задаётся один раз (проверяет и база)
        return readonly

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        company_id = request.user.insurance_company_id
        is_superuser = request.user.is_superuser
        adding = obj is None and bool(company_id)

        class UserForm(form):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                if adding:
                    # Новый пользователь сразу получает страховую того, кто его заводит.
                    self.instance.insurance_company_id = company_id
                kind = self.fields.get("kind")
                if kind is not None and not is_superuser:
                    # Администратором платформы пользователя делает только платформа.
                    kind.choices = [
                        (value, label) for value, label in kind.choices
                        if value != User.Kind.PLATFORM
                    ]

        return UserForm
