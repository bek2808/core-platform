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
from django.apps import apps
from django.contrib import admin
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import reverse

from .models import ChangeLog

AUDIT_FIELDS = ["created_at", "created_by", "updated_at", "updated_by"]


class NoRelatedLinks:
    """
    Убирает значки ✎ + 👁 у выпадающих списков (должность, банк, подразделение...).
    Справочники правятся в своих разделах админ-панели, а не мимоходом из
    карточки клиента или сотрудника; к тому же значки сбивали выравнивание полей.
    """

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        widget = getattr(formfield, "widget", None)
        if isinstance(widget, RelatedFieldWidgetWrapper):
            widget.can_add_related = False
            widget.can_change_related = False
            widget.can_view_related = False
            widget.can_delete_related = False
        return formfield


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
        # Сначала записи, с которых снимается «основной», потом остальные:
        # иначе при смене основного в одной форме база увидела бы на миг двух
        # основных и отказала (правило «основной - только один»).
        instances = sorted(formset.save(commit=False), key=lambda obj: bool(getattr(obj, "is_primary", False)))
        # Удаление строк (только у таблиц с allow_tracked_delete) - первым делом:
        # удалённый основной освобождает место новому основному в той же форме.
        # Каждое удаление пишется в журнал; у остальных таблиц оно запрещено.
        for obj in formset.deleted_objects:
            obj.delete_tracked(by=request.user)
        for obj in instances:
            obj.save(by=request.user)
        formset.save_m2m()

    def has_delete_permission(self, request, obj=None):
        return False

    def history_view(self, request, object_id, extra_context=None):
        # Вместо стандартной истории Django (только названия полей) -
        # наш журнал: было -> стало по каждому полю, вместе с вложенными
        # записями карточки (см. CardFilter).
        content_type = ContentType.objects.get_for_model(self.model)
        card = f"{content_type.pk}-{int(object_id)}"
        return redirect(f"{reverse('admin:audit_changelog_changelist')}?card={card}")


class CardFilter(admin.SimpleListFilter):
    """
    Фильтр «карточка» (?card=<таблица>-<номер>) - для кнопки «История».

    Показывает изменения самой карточки, таблиц с тем же номером записи
    (journal_same_number у модели: данные юрлица делят номер с карточкой
    контрагента) и вложенных записей, у которых эта карточка - хозяин
    (представители, счета).
    """

    title = "карточка"
    parameter_name = "card"

    def lookups(self, request, model_admin):
        value = self.value()
        return [(value, "выбранная карточка")] if value else []

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset
        try:
            type_id, object_id = (int(part) for part in value.split("-"))
            model = ContentType.objects.get_for_id(type_id).model_class()
        except (ValueError, ContentType.DoesNotExist):
            return queryset.none()
        same_number = [type_id] + [
            ContentType.objects.get_for_model(apps.get_model(label)).pk
            for label in getattr(model, "journal_same_number", ())
        ]
        return queryset.filter(
            Q(content_type_id__in=same_number, object_id=object_id)
            | Q(root_content_type_id=type_id, root_object_id=object_id)
        )


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
    # «таблица» - только те таблицы, по которым в журнале есть записи.
    list_filter = [
        CardFilter, "source", "action",
        ("content_type", admin.RelatedOnlyFieldListFilter), "changed_at",
    ]
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

    def get_queryset(self, request):
        # Мультиарендность: сотрудник видит изменения клиентов своей страховой
        # и общих справочников (банки, должности - без страховой).
        qs = super().get_queryset(request)
        company_id = request.user.insurance_company_id
        if company_id:
            qs = qs.filter(Q(insurance_company_id=company_id) | Q(insurance_company__isnull=True))
            # Видимость (часть B4): изменения записей, которых человек не видит
            # (агенты чужого подразделения), скрыты - и в «Истории» карточки тоже.
            from accounts.visibility import journal_hidden, scope_for

            hidden = journal_hidden(scope_for(request.user))
            return qs.exclude(hidden) if hidden is not None else qs
        if request.user.is_superuser:
            return qs  # администратор системы видит всё
        if getattr(request.user, "kind", None) == "platform":
            # Администратор платформы - только таблицы платформы: страховые
            # компании и должности сотрудников; данных страховых не видит (A4).
            platform_tables = ContentType.objects.get_for_models(
                apps.get_model("companies", "InsuranceCompany"), apps.get_model("accounts", "JobPosition"),
            ).values()
            return qs.filter(content_type__in=list(platform_tables))
        return qs.none()

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
