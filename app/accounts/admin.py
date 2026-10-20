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

Прикрепления (где и кем человек работает, с какой даты по какую) - блок
в карточке пользователя. Появляется после создания пользователя: сначала
учётная запись, потом прикрепление. Роли в прикреплении пишутся в журнал
отдельной строкой «роли: было -> стало» (Assignment.set_roles).

Должности сотрудников - общий справочник; роли - только просмотр
(каталог ролей меняется миграциями).
"""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Q
from django.forms.models import BaseInlineFormSet

from audit.admin import NoRelatedLinks, TrackedAdmin
from companies.models import Department

from .models import Assignment, JobPosition, Role, User

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


def overlaps(first, second):
    """Пересекаются ли два периода (с, по); «по» пусто - без конца, даты включительно."""
    (from_1, to_1), (from_2, to_2) = first, second
    return (to_2 is None or from_1 <= to_2) and (to_1 is None or from_2 <= to_1)


class AssignmentForm(forms.ModelForm):
    """
    Строка блока прикреплений.

    Проверку «периоды одного человека не пересекаются» строка с базой не
    делает: при переводе в одной отправке (старое закрыто датой «по», новое
    открыто) база ещё хранит старое открытым, и строка нового ошибочно
    увидела бы пересечение. Эту проверку делает весь блок сразу
    (AssignmentFormSet.clean) по новым значениям, а при сохранении её
    держит база.
    """

    class Meta:
        model = Assignment
        fields = "__all__"

    def validate_constraints(self):
        exclude = self._get_validation_exclusions() | {"user"}
        try:
            self.instance.validate_constraints(exclude=exclude)
        except ValidationError as error:
            self._update_errors(error)


class AssignmentFormSet(BaseInlineFormSet):
    """
    Прикрепления одного человека - блок в его карточке.

    Списки выбора - только своей страховой человека: подразделения
    и должности - действующие (плюс уже выбранное значение, если его
    закрыли), менеджеры ассистента - коллеги из той же страховой.

    «Периоды не пересекаются» проверяется по всему блоку сразу: каждая строка
    по отдельности не видит соседние новые строки, и без этой проверки
    сотрудник получил бы ошибку 500 из базы (ловушка из AGENTS.md).
    """

    def add_fields(self, form, index):
        # Вызывается для каждой строки, в том числе для пустого образца новой.
        super().add_fields(form, index)
        user = self.instance
        current = form.instance
        fields = form.fields
        if "department" in fields:
            fields["department"].queryset = Department.objects.filter(
                Q(status=Department.Status.ACTIVE) | Q(pk=current.department_id),
                insurance_company_id=user.insurance_company_id,
            ).select_related("parent")
            fields["department"].label_from_instance = lambda d: d.full_name()
        if "position" in fields:
            fields["position"].queryset = JobPosition.objects.filter(
                Q(is_active=True) | Q(pk=current.position_id)
            )
        if "assistant_to" in fields:
            fields["assistant_to"].queryset = User.objects.filter(
                insurance_company_id=user.insurance_company_id
            ).exclude(pk=user.pk)
            fields["assistant_to"].label_from_instance = lambda u: u.full_name()

    def clean(self):
        super().clean()
        periods = []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or not data.get("valid_from"):
                continue
            period = (data["valid_from"], data.get("valid_to"))
            for other in periods:
                if overlaps(period, other):
                    raise ValidationError(
                        "Периоды прикреплений пересекаются: на любую дату у человека "
                        "одно место. Закройте прежнее прикрепление датой «по» накануне нового."
                    )
            periods.append(period)


# Поля, которые стоят парами в одной строке. Пояснения под ними убраны:
# пояснение есть не у каждого поля пары, и соседи съезжали по высоте
# (так же в карточке контрагента, 20.10). Всё, что важно, - в подсказке блока.
ASSIGNMENT_ROWS = [
    ("valid_from", "valid_to"),
    ("department", "position"),
    ("visibility", "is_head"),
    ("assistant_to", "assistant_scope"),
]
ASSIGNMENT_HINT = (
    "«Действует по» - включительно; пусто - по сей день. На любую дату у человека одно "
    "прикрепление: при переводе закройте прежнее датой «по» накануне нового и добавьте новое. "
    "Руководитель видит данные своего подразделения; у подразделения на дату один руководитель. "
    "«Ассистент для» - менеджер, за которого человек вводит договоры."
)


class AssignmentInline(NoRelatedLinks, admin.StackedInline):
    model = Assignment
    fk_name = "user"
    form = AssignmentForm
    formset = AssignmentFormSet
    extra = 0
    can_delete = False  # не удаляются: закрываются датой «по»
    fieldsets = [
        (None, {
            "description": ASSIGNMENT_HINT,
            "fields": [*ASSIGNMENT_ROWS[:2], "roles", *ASSIGNMENT_ROWS[2:], "basis"],
        }),
    ]

    class Media:
        css = {"all": ["accounts/assignments.css"]}  # колонки одной ширины

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if any(db_field.name in row for row in ASSIGNMENT_ROWS):
            kwargs["help_text"] = ""
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "roles":
            kwargs["widget"] = forms.CheckboxSelectMultiple
        return super().formfield_for_manytomany(db_field, request, **kwargs)


@admin.register(User)
class UserAdmin(TrackedAdmin, BaseUserAdmin):
    inlines = [AssignmentInline]
    list_display = ["username", "full_name", "place", "kind", "insurance_company", "is_active", "is_staff"]
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
        current = Prefetch(
            "assignments",
            queryset=Assignment.objects.on().select_related("department__parent", "position"),
            to_attr="current_assignments",
        )
        return own_users(request, super().get_queryset(request)).prefetch_related(current)

    @admin.display(description="работает сейчас")
    def place(self, user):
        assignments = getattr(user, "current_assignments", None)
        if not assignments:
            return "—"
        assignment = assignments[0]
        return f"{assignment.department.full_name()}, {assignment.position}"

    def get_inline_instances(self, request, obj=None):
        # Прикрепление - после создания учётной записи: его страховая и
        # подразделения берутся от уже сохранённого пользователя.
        if obj is None:
            return []
        return super().get_inline_instances(request, obj)

    def save_formset(self, request, form, formset, change):
        if formset.model is not Assignment:
            return super().save_formset(request, form, formset, change)
        # По одной записи и с сотрудником - для журнала; роли - через
        # set_roles(), чтобы их смена тоже попала в журнал.
        forms_by_instance = {id(inline_form.instance): inline_form for inline_form in formset.forms}
        for assignment in formset.save(commit=False):
            assignment.save(by=request.user)
            roles = forms_by_instance[id(assignment)].cleaned_data.get("roles") or []
            assignment.set_roles(roles, by=request.user)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name in ("phone", "pinfl"):
            # Человек вводит «+998 (90) 123-45-67» или «3010 5780 1234 56»: лишние
            # знаки система убирает сама (User.normalize), а длину проверяет модель
            # уже по цифрам. Форма не должна резать ввод до этого.
            kwargs["max_length"] = 40
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        hidden = set()
        if not request.user.is_superuser:
            # Страховая у не суперпользователя - его собственная, выбирать нечего.
            hidden.add("insurance_company")
        if obj is not None and obj.kind != User.Kind.PLATFORM:
            # Права человека страховой - роли его прикрепления; группы
            # в учётной записи у него не действуют, показывать их - путать.
            hidden.add("groups")
        return [
            (name, {**options, "fields": [f for f in options["fields"] if f not in hidden]})
            for name, options in fieldsets
        ]

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        readonly += ["last_login", "date_joined", "is_staff"]  # «статус персонала» - от системы
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


@admin.register(JobPosition)
class JobPositionAdmin(TrackedAdmin):
    """
    Должности сотрудников: общий справочник всех страховых. Администратор
    страховой может только добавлять (права «добавить» и «смотреть»),
    переименовать и снять - платформа (право «изменить»).
    """

    list_display = ["name", "sort_order", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name"]
    fields = ["name", "sort_order", "is_active"]


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    """Каталог ролей - только просмотр: роли заводятся и меняются миграциями."""

    list_display = ["__str__", "code", "audience", "default_visibility", "description"]
    fields = ["group", "code", "audience", "default_visibility", "description", "sort_order"]
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
