from django.contrib import admin

from .models import InsuranceCompany


@admin.register(InsuranceCompany)
class InsuranceCompanyAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "country", "ersp_insurer", "ersp_region", "is_active"]
    list_filter = ["is_active", "country"]
    search_fields = ["name", "code"]
    autocomplete_fields = ["ersp_insurer", "ersp_region"]
    readonly_fields = ["created_at", "updated_at"]

    def get_readonly_fields(self, request, obj=None):
        # Краткий код задаётся один раз: на него будут опираться настройки и журналы.
        if obj is not None:
            return [*self.readonly_fields, "code"]
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        # Страховую компанию не удаляют: снимают отметку «работает».
        return False
