"""
Пользователи в админ-панели.

Стандартная форма Django + отчество, вид, телефон, ПИНФЛ и страховая компания.
Основа - audit.admin.TrackedAdmin: изменения пишутся в журнал с именем того,
кто сохранил; удаления нет (уволенного блокируют: снимают «активен»).

Мультиарендность: пользователь страховой видит и заводит только пользователей
своей страховой. Суперпользователь (аварийная техническая запись) видит всех
и выбирает страховую при создании. Страховая после создания не меняется.

Защита от повышения прав: не суперпользователь не видит и не меняет
«статус суперпользователя», группы и отдельные права и не видит учётных
записей суперпользователей (иначе сбросил бы такому пароль и вошёл под ним).
Свою карточку администратор страховой меняет частично: личные данные
и пароль - да, роли, место работы, вид и блокировку - нет (часть A3).

Кадровые кнопки в карточке (часть A3, правила - accounts/personnel.py):
«Принять», «Перевести», «Уволить», «Сбросить пароль». Подразделение,
должность и даты прикрепления меняются только ими; в блоке «Прикрепления»
правятся роли, видимость, руководитель, ассистент, основание.
Суперпользователь (аварийно) правит блок целиком.

Новый пользователь заводится одним экраном - сразу с местом работы.
Пароль не вводят: временный пароль выдаёт система и показывает один раз,
при первом входе человек задаёт свой (accounts/middleware.py).

«Последний администратор»: блокировка, увольнение, перевод и смена ролей
не должны оставить страховую без администратора (personnel.check_admin_remains).

Администратор платформы (вид «платформа», не суперпользователь; часть A4,
вариант «Б»): в списке видит пользователей всех страховых (без
суперпользователей), открывает карточку только администраторов страховых
(блокировка, личные данные, «Сбросить пароль») и людей платформы (только
просмотр, кроме своей). Место работы и роли людей страховых не меняет;
администратора страховой заводит из карточки страховой
(companies.admin, «Завести администратора»).

Прикрепления (где и кем человек работает, с какой даты по какую) - блок
в карточке пользователя. Появляется после создания пользователя: сначала
учётная запись, потом прикрепление. Роли в прикреплении пишутся в журнал
отдельной строкой «роли: было -> стало» (Assignment.set_roles).

Должности сотрудников - общий справочник; роли - только просмотр
(каталог ролей меняется миграциями).
"""
from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch, Q
from django.forms.models import BaseInlineFormSet
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils import timezone

from audit.admin import NoRelatedLinks, TrackedAdmin
from companies.models import Department

from . import personnel
from .forms import DismissForm, HireForm, OwnPasswordChangeForm, TransferForm, UserAddForm
from .models import Assignment, JobPosition, Role, User, current_assignment

# Смена своего пароля: в журнал - от имени самого человека, отметка
# «сменить пароль при входе» снимается.
admin.site.password_change_form = OwnPasswordChangeForm

# Где человек работает - меняется только кадровыми кнопками (у суперпользователя -
# и прямо в блоке): история места работы не переписывается задним числом.
PLACE_FIELDS = ["valid_from", "valid_to", "department", "position"]

# Что не суперпользователю менять нельзя: иначе администратор страховой
# мог бы сделать суперпользователем себя или кого-то ещё.
PRIVILEGE_FIELDS = ["is_superuser", "groups", "user_permissions"]


def is_platform(request):
    """Администратор платформы (не суперпользователь)."""
    return request.user.kind == User.Kind.PLATFORM and not request.user.is_superuser


def own_users(request, queryset):
    """
    Своя страховая (без суперпользователей); платформа - все страховые
    (без суперпользователей); суперпользователь - все; иначе - никто.
    """
    if request.user.is_superuser:
        company_id = request.user.insurance_company_id
        return queryset.filter(insurance_company_id=company_id) if company_id else queryset
    company_id = request.user.insurance_company_id
    if company_id:
        return queryset.filter(insurance_company_id=company_id, is_superuser=False)
    if is_platform(request):
        return queryset.filter(is_superuser=False)
    return queryset.none()


def is_company_admin(user):
    """Администратор страховой: роль «Администратор страховой» в действующем прикреплении."""
    assignments = getattr(user, "current_assignments", None)
    if assignments is None:
        assignments = list(Assignment.objects.on().filter(user=user).prefetch_related("roles"))
    return any(personnel.is_admin_role_set(a.roles.all()) for a in assignments)


def platform_may_open(user):
    """Чью карточку открывает платформа: администраторов страховых и людей платформы."""
    return user.kind == User.Kind.PLATFORM or is_company_admin(user)


class RoleFilter(admin.SimpleListFilter):
    """Фильтр «роль»: у людей страховой - по действующему прикреплению, у платформы - по группе."""

    title = "роль"
    parameter_name = "role"

    def lookups(self, request, model_admin):
        return [(role.code, str(role)) for role in Role.objects.select_related("group")]

    def queryset(self, request, queryset):
        if not self.value():
            return queryset
        current = Assignment.objects.on().filter(roles__code=self.value()).values("user_id")
        return queryset.filter(
            Q(pk__in=current) | Q(kind=User.Kind.PLATFORM, groups__role__code=self.value())
        ).distinct()


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
        self._check_admin_remains()

    def _check_admin_remains(self):
        """Смена ролей в блоке не должна оставить страховую без администратора."""
        user = self.instance
        if user.pk is None:
            return
        after = []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None) or {}
            current = form.instance
            valid_from = data.get("valid_from", current.valid_from)
            if not valid_from:
                continue
            roles = data["roles"] if "roles" in data else (current.roles.all() if current.pk else [])
            after.append(personnel.Period(
                valid_from, data.get("valid_to", current.valid_to), personnel.is_admin_role_set(roles),
            ))
        personnel.check_admin_remains(user, after, user.is_active)


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
    "Где человек работает (даты, подразделение, должность) меняется кнопками вверху карточки: "
    "«Принять», «Перевести», «Уволить». Здесь правятся роли, видимость, руководитель, "
    "ассистент и основание. «Действует по» - включительно; пусто - по сей день. "
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

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            readonly += PLACE_FIELDS
        return readonly

    def has_add_permission(self, request, obj=None):
        # Новое место - кнопками «Принять» / «Перевести»; вручную - только аварийно.
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        if is_own_card(request, obj):
            return False  # свои роли администратор не меняет (решение 21.10)
        return super().has_change_permission(request, obj)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "roles":
            kwargs["widget"] = forms.CheckboxSelectMultiple
        return super().formfield_for_manytomany(db_field, request, **kwargs)


def is_own_card(request, user):
    """Своя карточка у не суперпользователя - правится частично."""
    return user is not None and user.pk == request.user.pk and not request.user.is_superuser


ADD_PLACE_HINT = (
    "Где человек будет работать. Без места сотрудник страховой не сможет войти. "
    "Администратору платформы место не нужно."
)


@admin.register(User)
class UserAdmin(TrackedAdmin, BaseUserAdmin):
    inlines = [AssignmentInline]
    list_display = ["username", "full_name", "place", "kind", "insurance_company", "is_active", "is_staff"]
    list_filter = ["kind", "is_active", RoleFilter, "insurance_company"]
    search_fields = ["username", "first_name", "last_name", "patronymic", "email", "phone", "pinfl"]
    ordering = ["username"]

    fieldsets = [
        (None, {"fields": ["username", "password"]}),
        ("Личные данные", {"fields": ["last_name", "first_name", "patronymic", "email", "phone", "pinfl"]}),
        ("Принадлежность", {"fields": ["kind", "insurance_company"]}),
        ("Права", {"fields": [
            "is_active", "must_change_password", "is_staff", "is_superuser", "groups", "user_permissions",
        ]}),
        ("Даты", {"fields": ["last_login", "date_joined"]}),
    ]
    add_form = UserAddForm
    add_form_template = None  # обычная форма Django (у стандартной - текст про ввод пароля)
    add_fieldsets = [
        (None, {"fields": ["username"]}),
        ("Личные данные", {"fields": ["last_name", "first_name", "patronymic", "email", "phone", "pinfl"]}),
        ("Принадлежность", {"fields": ["kind", "insurance_company"]}),
        ("Место работы", {"description": ADD_PLACE_HINT, "fields": UserAddForm.PLACE_FIELDS}),
    ]

    def get_queryset(self, request):
        current = Prefetch(
            "assignments",
            queryset=Assignment.objects.on().select_related("department__parent", "position").prefetch_related("roles"),
            to_attr="current_assignments",
        )
        return own_users(request, super().get_queryset(request)).prefetch_related(current)

    # --- платформа (A4, вариант «Б») ---

    def get_list_display(self, request):
        if is_platform(request):
            # Вид - в фильтре справа: иначе таблица шире окна и «карточка» уходит за край.
            return ["username", "full_name", "insurance_company", "is_active", "last_login_short", "card_link"]
        return self.list_display

    def get_list_display_links(self, request, list_display):
        if is_platform(request):
            return None  # карточки открываются не у всех - ссылка в колонке «карточка»
        return super().get_list_display_links(request, list_display)

    @admin.display(description="последний вход", ordering="last_login")
    def last_login_short(self, user):
        if user.last_login is None:
            return "—"
        return timezone.localtime(user.last_login).strftime("%d.%m.%Y %H:%M")

    @admin.display(description="карточка")
    def card_link(self, user):
        if not platform_may_open(user):
            return "—"
        return format_html('<a href="{}">открыть</a>', reverse("admin:accounts_user_change", args=[user.pk]))

    def has_view_permission(self, request, obj=None):
        if obj is not None and is_platform(request) and not platform_may_open(obj):
            return False
        return super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj is not None and is_platform(request):
            if not platform_may_open(obj):
                return False
            if obj.kind == User.Kind.PLATFORM and obj.pk != request.user.pk:
                return False  # людей платформы меняет только суперпользователь (решение 19)
        return super().has_change_permission(request, obj)

    def has_add_permission(self, request):
        if is_platform(request):
            return False  # администратора страховой - из карточки страховой
        return super().has_add_permission(request)

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
        readonly += ["must_change_password"]  # ставит и снимает система
        if not request.user.is_superuser:
            readonly += PRIVILEGE_FIELDS
        if is_own_card(request, obj):
            readonly += ["is_active", "kind"]  # себя не блокирует и вид себе не меняет
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
                if obj is None:
                    # Места работы - своей страховой (суперпользователю - всех).
                    self.limit_places(company_id)
                kind = self.fields.get("kind")
                if kind is not None and not is_superuser:
                    # Администратором платформы пользователя делает только платформа.
                    kind.choices = [
                        (value, label) for value, label in kind.choices
                        if value != User.Kind.PLATFORM
                    ]

            def clean(self):
                data = super().clean()
                # Блокировка не должна оставить страховую без администратора.
                if self.instance.pk and "is_active" in data and not data["is_active"]:
                    try:
                        personnel.check_admin_remains(self.instance, personnel.periods_of(self.instance), False)
                    except ValidationError as error:
                        self.add_error("is_active", error)
                return data

        return UserForm

    # --- новый пользователь: временный пароль и место работы ---

    def save_model(self, request, obj, form, change):
        if change:
            return super().save_model(request, obj, form, change)
        obj._issued_password = personnel.generate_password(obj)
        obj.set_password(obj._issued_password)
        obj.must_change_password = True
        obj.save(by=request.user)
        data = form.cleaned_data
        if obj.insurance_company_id is not None and data.get("department") is not None:
            # Форма уже проверила место; ошибка здесь откатит и учётную запись.
            personnel.hire(obj, valid_from=data["valid_from"], place=form.place(),
                           roles=data.get("roles") or [], by=request.user)

    def response_add(self, request, obj, post_url_continue=None):
        # Вместо карточки - страница с временным паролем (показывается один раз).
        return self.render_password(request, obj, obj._issued_password)

    # --- кадровые кнопки ---

    def get_urls(self):
        own = [
            path("<path:object_id>/hire/", self.admin_site.admin_view(self.hire_view), name="accounts_user_hire"),
            path("<path:object_id>/transfer/", self.admin_site.admin_view(self.transfer_view),
                 name="accounts_user_transfer"),
            path("<path:object_id>/dismiss/", self.admin_site.admin_view(self.dismiss_view),
                 name="accounts_user_dismiss"),
            path("<path:object_id>/reset-password/", self.admin_site.admin_view(self.reset_password_view),
                 name="accounts_user_reset_password"),
        ]
        return own + super().get_urls()

    def personnel_actions(self, request, user):
        """Кнопки, доступные в карточке этого пользователя."""
        if user is None or not self.has_change_permission(request, user):
            return []
        actions = []
        if user.pk != request.user.pk:
            actions.append(("accounts_user_reset_password", "Сбросить пароль"))
        if self._personnel_allowed(request, user):
            today_place = current_assignment(user)
            has_future = user.assignments.filter(valid_from__gt=timezone.localdate()).exists()
            if today_place is None and not has_future:
                actions.insert(0, ("accounts_user_hire", "Принять"))
            if today_place is not None:
                actions[0:0] = [("accounts_user_transfer", "Перевести"), ("accounts_user_dismiss", "Уволить")]
        return [{"url": reverse(f"admin:{name}", args=[user.pk]), "label": label} for name, label in actions]

    def change_view(self, request, object_id, form_url="", extra_context=None):
        user = self.get_object(request, unquote(object_id))
        extra_context = {**(extra_context or {}), "personnel_actions": self.personnel_actions(request, user)}
        return super().change_view(request, object_id, form_url, extra_context)

    @staticmethod
    def _personnel_allowed(request, user):
        # Место работы - у людей страховой; своё администратор страховой не меняет;
        # платформа места не меняет вовсе (этим ведает сама страховая).
        return user.insurance_company_id is not None and not is_own_card(request, user) and not is_platform(request)

    def _target(self, request, object_id, personnel_action=True):
        user = self.get_object(request, unquote(object_id))
        if user is None:
            raise Http404("Пользователь не найден.")
        if not self.has_change_permission(request, user):
            raise PermissionDenied
        if personnel_action and not self._personnel_allowed(request, user):
            raise PermissionDenied
        return user

    def _render_action(self, request, user, form, title, submit_label, intro=()):
        place = current_assignment(user)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": f"{title}: {user.full_name()}",
            "target": user,
            "form": form,
            # Обычная разметка формы админ-панели (как в карточках): подписи, галочки, пояснения.
            "adminform": admin.helpers.AdminForm(form, [(None, {"fields": list(form.fields)})], {})
            if form.fields else [],
            "media": self.media + form.media,
            "submit_label": submit_label,
            "intro": intro,
            "place": f"{place.department.full_name()}, {place.position}" if place else "",
            "change_url": reverse("admin:accounts_user_change", args=[user.pk]),
        }
        return TemplateResponse(request, "admin/accounts/user/action.html", context)

    def _run(self, request, user, form, action, done):
        """Выполнить действие; ошибки правил - к полям формы."""
        try:
            action()
        except ValidationError as error:
            if hasattr(error, "error_dict"):
                for field, errors in error.error_dict.items():
                    form.add_error(field if field in form.fields else None, errors)
            else:
                form.add_error(None, error)
            return None
        messages.success(request, done)
        return redirect("admin:accounts_user_change", user.pk)

    def hire_view(self, request, object_id):
        user = self._target(request, object_id)
        form = HireForm(request.POST or None)
        form.limit_places(user.insurance_company_id)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            done = self._run(request, user, form, lambda: personnel.hire(
                user, valid_from=data["valid_from"], valid_to=data.get("valid_to"), place=form.place(),
                roles=data["roles"], by=request.user,
            ), f"{user.full_name()}: принят(а) с {data['valid_from']:%d.%m.%Y}.")
            if done:
                return done
        return self._render_action(request, user, form, "Принять", "Принять", [
            "Новое место работы с указанной даты. Если учётная запись заблокирована - блокировка снимется; "
            "войти человек сможет с даты начала.",
        ])

    def transfer_view(self, request, object_id):
        user = self._target(request, object_id)
        place = current_assignment(user)
        initial = {}
        if place is not None:
            initial = {
                "department": place.department_id, "position": place.position_id,
                "roles": list(place.roles.all()), "visibility": place.visibility, "is_head": place.is_head,
            }
        form = TransferForm(request.POST or None, initial=initial)
        form.limit_places(user.insurance_company_id)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            done = self._run(request, user, form, lambda: personnel.transfer(
                user, valid_from=data["valid_from"], place=form.place(), roles=data["roles"], by=request.user,
            ), f"{user.full_name()}: переведён(а) с {data['valid_from']:%d.%m.%Y}.")
            if done:
                return done
        return self._render_action(request, user, form, "Перевести", "Перевести", [
            "Нынешнее место закроется накануне даты перевода, новое начнётся с неё. "
            "Роли по умолчанию - нынешние; права меняются в день перевода.",
        ])

    def dismiss_view(self, request, object_id):
        user = self._target(request, object_id)
        form = DismissForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            day = form.cleaned_data["last_day"]
            done = self._run(request, user, form, lambda: personnel.dismiss(user, last_day=day, by=request.user),
                             f"{user.full_name()}: уволен(а), последний день - {day:%d.%m.%Y}.")
            if done:
                return done
        return self._render_action(request, user, form, "Уволить", "Уволить", [
            "Место работы закроется последним днём. Учётная запись не удаляется: "
            "история и записи человека остаются.",
        ])

    def reset_password_view(self, request, object_id):
        user = self._target(request, object_id, personnel_action=False)
        if user.pk == request.user.pk:
            return redirect("admin:password_change")  # свой пароль - сменить, а не сбросить
        if request.method == "POST":
            return self.render_password(request, user, personnel.issue_temporary_password(user, by=request.user))
        return self._render_action(request, user, forms.Form(), "Сбросить пароль", "Выдать временный пароль", [
            "Система придумает временный пароль и покажет его один раз. Нынешний пароль человека "
            "перестанет действовать; при входе он задаст свой.",
        ])

    def user_change_password(self, request, id, form_url=""):
        # Ссылка «Сбросить пароль» под полем пароля: вместо ввода пароля
        # администратором - временный пароль от системы (решение 21.10).
        return redirect("admin:accounts_user_reset_password", unquote(id))

    def render_password(self, request, user, password):
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": "Временный пароль",
            "target": user,
            "password": password,
            "change_url": reverse("admin:accounts_user_change", args=[user.pk]),
        }
        response = TemplateResponse(request, "admin/accounts/user/password_issued.html", context)
        response["Cache-Control"] = "no-store"
        return response


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
