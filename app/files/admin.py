"""
Журнал просмотров файлов в админ-панели (docs/files-model.md): кто, когда и какой
файл открыл. Только для чтения - его пишет сама система при открытии файла
(files/views.py), править и удалять нельзя (триггер в базе).

Мультиарендность и видимость: администратор страховой видит просмотры файлов
своей страховой. Журнал общий для всех назначений файлов, поэтому показывается
только при видимости «вся страховая» (иначе пришлось бы по каждому просмотру
решать, видит ли человек запись, к которой приложен файл). Администратор
платформы данных страховых не видит - и права у него нет.
"""
from django.contrib import admin

from accounts.visibility import Kind, scope_for

from . import registry
from .display import size_text
from .models import FileView


@admin.register(FileView)
class FileViewAdmin(admin.ModelAdmin):
    list_display = ["viewed_at", "who", "file_name", "what", "size"]
    list_filter = ["file__purpose"]
    search_fields = ["file__original_name", "user__username", "user__last_name"]
    date_hierarchy = "viewed_at"
    list_select_related = ["file", "user"]
    ordering = ["-viewed_at", "-id"]

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        scope = scope_for(request.user)
        if scope.kind not in (Kind.ALL, Kind.COMPANY):
            return queryset.none()
        return scope.filter(queryset, company="file__insurance_company_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="кто", ordering="user__last_name")
    def who(self, view):
        return view.user.full_name() if view.user_id else "система"

    @admin.display(description="файл", ordering="file__original_name")
    def file_name(self, view):
        return view.file.original_name

    @admin.display(description="что это")
    def what(self, view):
        try:
            return f"{registry.get(view.file.purpose).label} № {view.file.owner_id}"
        except KeyError:
            return f"{view.file.purpose} № {view.file.owner_id}"

    @admin.display(description="размер")
    def size(self, view):
        return size_text(view.file.size)
