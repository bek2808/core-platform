"""
Общие настройки админ-панели для таблиц с аудитом (audit.models.TrackedModel).

TrackedAdmin:
- при сохранении передаёт в запись сотрудника, который нажал «Сохранить»;
- показывает поля «создал / изменил» только для чтения;
- запрещает удаление: в Core-системе записи не удаляют, а выводят из
  работы (архив, «не действует»), потому что на них ссылаются полисы.
"""
from django.contrib import admin

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

    def has_delete_permission(self, request, obj=None):
        return False
