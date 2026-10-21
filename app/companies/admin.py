"""
Страховые компании и их подразделения в админ-панели.

Мультиарендность у подразделений: сотрудник видит и заводит подразделения
только своей страховой; страховая подставляется из его учётной записи.
Суперпользователь (аварийная техническая запись) видит все и выбирает
страховую при создании. Пользователь без страховой и не суперпользователь
не видит подразделений - так работает администратор платформы (часть A4).

Страховые компании (часть A4) - экран администратора платформы: счётчики
(пользователи работают / заблокированы, подразделения, последний вход),
ссылка «Пользователи (N)», журнал изменений карточки, кнопка «Завести
администратора» (место - головной офис, роль «Администратор страховой»,
временный пароль от системы).
"""
from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from audit.admin import NoRelatedLinks, TrackedAdmin

from .models import Department, InsuranceCompany


@admin.register(InsuranceCompany)
class InsuranceCompanyAdmin(NoRelatedLinks, TrackedAdmin):
    # Таблица - узкая, чтобы помещалась рядом с фильтром: страна - в фильтре,
    # краткий код - в карточке, люди - одной колонкой-ссылкой на их список.
    list_display = ["name", "is_active", "users_link", "departments_count", "last_login"]
    list_filter = ["is_active", "country"]
    search_fields = ["name", "code"]
    autocomplete_fields = ["ersp_insurer", "ersp_region"]
    fields = ["name", "code", "country", "ersp_insurer", "ersp_region", "is_active"]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            working=Count("users", filter=Q(users__is_active=True, users__is_superuser=False), distinct=True),
            blocked=Count("users", filter=Q(users__is_active=False, users__is_superuser=False), distinct=True),
            departments_total=Count("departments", distinct=True),
            last_user_login=Max("users__last_login"),
        )

    @admin.display(description="подразделений", ordering="departments_total")
    def departments_count(self, company):
        return company.departments_total

    @admin.display(description="последний вход", ordering="last_user_login")
    def last_login(self, company):
        if company.last_user_login is None:
            return "—"
        return timezone.localtime(company.last_user_login).strftime("%d.%m.%Y %H:%M")

    @admin.display(description="пользователи: работают / заблокированы", ordering="working")
    def users_link(self, company):
        url = reverse("admin:accounts_user_changelist")
        return format_html(
            '<a href="{}?insurance_company__id__exact={}">{} / {}</a>', url, company.pk, company.working, company.blocked,
        )

    def get_readonly_fields(self, request, obj=None):
        # Краткий код задаётся один раз: на него будут опираться настройки и журналы.
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            readonly.append("code")
        return readonly

    # --- «Завести администратора» ---

    def get_urls(self):
        own = [
            path("<path:object_id>/add-admin/", self.admin_site.admin_view(self.add_admin_view),
                 name="companies_insurancecompany_add_admin"),
        ]
        return own + super().get_urls()

    def change_view(self, request, object_id, form_url="", extra_context=None):
        company = self.get_object(request, unquote(object_id))
        extra_context = {**(extra_context or {}), "can_add_admin": self._can_add_admin(request, company)}
        return super().change_view(request, object_id, form_url, extra_context)

    def _can_add_admin(self, request, company):
        return (
            company is not None and company.is_active
            and self.has_change_permission(request, company) and request.user.has_perm("accounts.add_user")
        )

    def add_admin_view(self, request, object_id):
        from accounts import personnel
        from accounts.forms import CompanyAdminForm
        from accounts.models import Role, User

        company = self.get_object(request, unquote(object_id))
        if company is None:
            raise Http404("Страховая не найдена.")
        if not self._can_add_admin(request, company):
            raise PermissionDenied
        change_url = reverse("admin:companies_insurancecompany_change", args=[company.pk])
        form = CompanyAdminForm(request.POST or None, instance=User(insurance_company=company, kind=User.Kind.EMPLOYEE))
        if request.method == "POST" and form.is_valid():
            try:
                with transaction.atomic():
                    user = form.save(commit=False)
                    password = personnel.generate_password(user)
                    user.set_password(password)
                    user.must_change_password = True
                    user.save(by=request.user)
                    personnel.hire(
                        user, valid_from=form.cleaned_data["valid_from"], by=request.user,
                        place={"department": Department.head_office_for(company),
                               "position": form.cleaned_data["position"]},
                        roles=[Role.objects.get(code=personnel.COMPANY_ADMIN_ROLE)],
                    )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, f"{company}: заведён администратор {user.full_name()}.")
                return self.admin_site._registry[User].render_password(request, user, password)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": f"Завести администратора: {company}",
            "company": company,
            "form": form,
            "adminform": admin.helpers.AdminForm(form, [(None, {"fields": list(form.fields)})], {}),
            "media": self.media + form.media,
            "change_url": change_url,
        }
        return TemplateResponse(request, "admin/companies/insurancecompany/add_admin.html", context)


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
