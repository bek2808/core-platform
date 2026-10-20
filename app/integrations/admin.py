"""
Журнал обмена в админ-панели: только просмотр, даже у суперпользователя.

Мультиарендность: сотрудник видит обмен своей страховой компании;
администратор системы (без страховой) - весь журнал.
"""
import json

from django.contrib import admin
from django.utils.html import format_html

from .models import Exchange


def pretty(value):
    if value is None:
        return "—"
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return format_html('<pre style="white-space: pre-wrap; margin: 0">{}</pre>', text)


@admin.register(Exchange)
class ExchangeAdmin(admin.ModelAdmin):
    list_display = [
        "started_at", "environment", "operation", "subject", "outcome",
        "http_status", "duration_ms", "who",
    ]
    list_filter = ["environment", "operation", "outcome", "started_at"]
    search_fields = ["subject", "error_message"]
    search_help_text = "ИНН (или другой предмет запроса) и текст ошибки."
    list_select_related = ["user", "insurance_company"]
    date_hierarchy = "started_at"
    fields = [
        "started_at", "who", "insurance_company", "environment", "operation", "subject",
        "method", "url", "request", "http_status", "duration_ms", "outcome",
        "error_code", "error_message", "response", "response_text",
    ]
    readonly_fields = fields

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        company_id = request.user.insurance_company_id
        if company_id:
            return qs.filter(insurance_company_id=company_id)
        if request.user.is_superuser:
            return qs  # администратор системы видит всё
        return qs.none()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="кто", ordering="user__username")
    def who(self, exchange):
        return exchange.user or "система"

    @admin.display(description="запрос")
    def request(self, exchange):
        return pretty(exchange.request_body)

    @admin.display(description="ответ")
    def response(self, exchange):
        return pretty(exchange.response_body)
