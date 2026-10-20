"""
Страховые компании и их подразделения в админ-панели.

Мультиарендность у подразделений: сотрудник видит и заводит подразделения
только своей страховой; страховая подставляется из его учётной записи.
Суперпользователь (аварийная техническая запись) видит все и выбирает
страховую при создании. Пользователь без страховой и не суперпользователь
не видит ничего - так будет работать администратор платформы (часть A4).
"""
from django import forms
from django.contrib import admin

from audit.admin import TrackedAdmin

from .models import Department, InsuranceCompany


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


def own_departments(request, queryset):
    """Своя страховая; суперпользователь - все; иначе - ничего."""
    company_id = request.user.insurance_company_id
    if company_id:
        return queryset.filter(insurance_company_id=company_id)
    if request.user.is_superuser:
        return queryset
    return queryset.none()


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        kind = self.fields.get("kind")
        if kind is not None:
            # Головной офис создаёт система вместе со страховой - вручную
            # заводятся только филиалы и отделы.
            kind.choices = [
                (value, label) for value, label in kind.choices
                if value != Department.Kind.HEAD_OFFICE
            ]


@admin.register(Department)
class DepartmentAdmin(TrackedAdmin):
    form = DepartmentForm
    list_display = ["full_name", "kind", "code", "effective_region", "status"]
    list_filter = ["kind", "status"]
    search_fields = ["name", "code"]
    list_select_related = [
        "parent__region", "parent__insurance_company__ersp_region", "insurance_company__ersp_region", "region",
    ]
    autocomplete_fields = ["region"]

    def get_queryset(self, request):
        return own_departments(request, super().get_queryset(request))

    def get_fields(self, request, obj=None):
        fields = ["kind", "parent", "name", "code", "region"]
        if obj is not None:
            fields = [*fields, "status"]
        if not request.user.insurance_company_id:
            fields = ["insurance_company", *fields]
        return fields

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            # Страховая, вид и «входит в» задаются один раз (проверяет и база).
            readonly += ["insurance_company", "kind", "parent"]
            if obj.kind != Department.Kind.BRANCH:
                readonly += ["region"]  # регион берётся у страховой или у филиала
        return readonly

    def get_list_display(self, request):
        if request.user.insurance_company_id:
            return self.list_display
        return [*self.list_display, "insurance_company"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "parent":
            # Отдел входит в действующий головной офис или филиал своей страховой.
            kwargs["queryset"] = own_departments(
                request,
                Department.objects.filter(
                    kind__in=[Department.Kind.HEAD_OFFICE, Department.Kind.BRANCH],
                    status=Department.Status.ACTIVE,
                ),
            ).select_related("insurance_company")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        company_id = request.user.insurance_company_id
        if obj is None and company_id:
            # Новое подразделение сразу получает страховую сотрудника - ещё
            # до проверки формы, чтобы проверки кода и названия знали, где искать.
            class FormWithCompany(form):
                def __init__(self, *args, **kw):
                    super().__init__(*args, **kw)
                    self.instance.insurance_company_id = company_id

            return FormWithCompany
        return form
