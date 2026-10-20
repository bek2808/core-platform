"""
Админ-панель аудита.

TrackedAdmin - основа для таблиц с аудитом (audit.models.TrackedModel):
- при сохранении передаёт в запись сотрудника, который нажал «Сохранить»;
- показывает поля «создал / изменил» только для чтения;
- запрещает удаление: записи выводят из работы (архив, «не действует»),
  потому что на них ссылаются полисы;
- кнопка «История» в карточке ведёт в журнал изменений этой записи.

ChangeLogAdmin - журнал изменений: только просмотр, даже у суперпользователя.
"""
from urllib.parse import urlencode

from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models
from django.shortcuts import redirect
from django.urls import reverse

from .models import ChangeLog

AUDIT_FIELDS = ["created_at", "created_by", "updated_at", "updated_by"]


class TrackedAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [*super().get_readonly_fields(request, obj), *AUDIT_FIELDS]

    def get_fieldsets(self, request, obj=None):
        # Поля аудита - отдельным свёрнутым блоком внизу карточки.
        fieldsets = super().get_fieldsets(request, obj)
        main = [
            (name, {**options, "fields": [f for f in options["fields"] if f not in AUDIT_FIELDS]})
            for name, options in fieldsets
        ]
        if obj is None:
            return main
        return [*main, ("Кто и когда", {"fields": AUDIT_FIELDS, "classes": ["collapse"]})]

    def save_model(self, request, obj, form, change):
        obj.save(by=request.user)

    def save_formset(self, request, form, formset, change):
        # Вложенные блоки карточки (данные юрлица, позже представители
        # и счета) сохраняются по одной записи и с сотрудником - иначе
        # их изменения прошли бы мимо журнала.
        for obj in formset.save(commit=False):
            obj.save(by=request.user)
        formset.save_m2m()

    def has_delete_permission(self, request, obj=None):
        return False

    def history_view(self, request, object_id, extra_context=None):
        # Вместо стандартной истории Django (только названия полей) -
        # наш журнал: было -> стало по каждому полю.
        content_type = ContentType.objects.get_for_model(self.model)
        query = urlencode({"content_type__id__exact": content_type.pk, "object_id": object_id})
        return redirect(f"{reverse('admin:audit_changelog_changelist')}?{query}")


def field_label(entry):
    """Название поля, как его видит сотрудник («МФО», а не mfo)."""
    model = entry.content_type.model_class()
    try:
        return model._meta.get_field(entry.field).verbose_name
    except (AttributeError, FieldDoesNotExist):
        return entry.field  # поле уже удалено из модели - показываем как есть


def value_text(entry, value):
    """Значение для показа: да/нет, прочерк для пустого, название для ссылки."""
    if value is None or value == "":
        return "—"
    if value is True:
        return "да"
    if value is False:
        return "нет"
    model = entry.content_type.model_class()
    try:
        field = model._meta.get_field(entry.field)
    except (AttributeError, FieldDoesNotExist):
        return str(value)
    if isinstance(field, models.ForeignKey):
        # В журнале хранится номер связанной записи; показываем и её название.
        try:
            related = field.related_model._base_manager.filter(pk=value).first()
        except (ValueError, TypeError, ValidationError):
            # Строка записана, когда поле было другого типа (например, валюта
            # капитала до миграции counterparties/0004 хранилась текстом «UZS»).
            # Журнал не переписывается - показываем значение как было.
            return str(value)
        return f"{related} (№ {value})" if related else f"№ {value}"
    if field.choices:
        return dict(field.flatchoices).get(value, value)
    return str(value)


@admin.register(ChangeLog)
class ChangeLogAdmin(admin.ModelAdmin):
    list_display = [
        "changed_at",
        "who",
        "source",
        "table",
        "object_repr",
        "field_name",
        "old",
        "new",
    ]
    list_filter = ["source", "action", "content_type", "changed_at"]
    search_fields = ["object_repr", "user__username"]
    list_select_related = ["user", "content_type"]
    date_hierarchy = "changed_at"
    fields = [
        "changed_at",
        "who",
        "source",
        "action",
        "table",
        "object_id",
        "object_repr",
        "insurance_company",
        "field_name",
        "old",
        "new",
        "change_id",
    ]
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="кто", ordering="user__username")
    def who(self, entry):
        return entry.user or "система"

    @admin.display(description="таблица", ordering="content_type")
    def table(self, entry):
        model = entry.content_type.model_class()
        return model._meta.verbose_name if model else entry.content_type.model

    @admin.display(description="поле")
    def field_name(self, entry):
        return field_label(entry)

    @admin.display(description="было")
    def old(self, entry):
        return value_text(entry, entry.old_value)

    @admin.display(description="стало")
    def new(self, entry):
        return value_text(entry, entry.new_value)
