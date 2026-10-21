"""
Агенты в админ-панели (часть B2, модель: docs/users-model.md, «Агенты (этап B)»).

Экран «Агенты»:
- список: агент, вид, работает ли сегодня, подразделение сейчас, agencyId;
- новый агент - одним экраном: контрагент (юрлицо своей страховой, ещё
  не агент), вид, данные для ЕРСП и сразу место - подразделение с даты.
  Соглашения заводятся в карточке после сохранения;
- карточка: блоки «Соглашения» (правятся; прекращение - только кнопкой),
  «Места» (только для чтения: место меняется кнопками) и «Связь с ЕРСП»;
- кнопки: «Прикрепить» (места нет), «Перевести» (место есть),
  «Прекратить соглашение» (есть действующее соглашение), «Завести
  пользователя»; в блоке «Пользователи агента» у каждого - «Сбросить
  пароль» и «Отключить». Сами действия - в agents/services.py, экран лишь
  собирает данные.

Мультиарендность: сотрудник видит и заводит агентов только своей
страховой; суперпользователь (аварийная запись) - всех; администратор
платформы - никого (и прав у него нет).

Блок «Связь с ЕРСП» (ErspLinkInline) - общий для агента и подразделения:
companies/admin.py берёт его отсюда.
"""
from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.contrib.admin.widgets import AdminDateWidget
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html_join

from accounts.models import User
from audit.admin import NoRelatedLinks, TrackedAdmin
from companies.models import Department
from counterparties.models import BankAccount, Counterparty
from integrations.models import ErspAgency

from . import services
from .models import Agent, Agreement, ErspLink, Member, Placement


def own_rows(request, queryset, field="insurance_company_id"):
    """Своя страховая; суперпользователь - все; иначе (платформа) - ничего."""
    company_id = request.user.insurance_company_id
    if company_id:
        return queryset.filter(**{field: company_id})
    if request.user.is_superuser:
        return queryset
    return queryset.none()


def _today():
    return timezone.localdate()


def department_choices(company_id):
    return Department.objects.filter(
        insurance_company_id=company_id, status=Department.Status.ACTIVE,
    ).select_related("parent")


# --- Связь с ЕРСП (общий блок агента и подразделения) ----------------------------


class AgencyChoice(forms.ModelChoiceField):
    def label_from_instance(self, agency):
        kind = agency.get_agency_type_display() if agency.agency_type else "вид не указан"
        return f"{agency.ersp_id} - {agency.name} ({kind}, {agency.get_environment_display()})"


class ErspLinkForm(forms.ModelForm):
    class Meta:
        model = ErspLink
        fields = ["ersp_agency", "is_active"]
        field_classes = {"ersp_agency": AgencyChoice}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields.get("ersp_agency")
        if field is None:
            return
        if self.instance.pk:
            # Связь не переносится на другую запись ЕРСП (проверяет и база).
            field.disabled = True
        else:
            # Для новой связи - только живые записи ЕРСП, ещё ни с чем не связанные.
            field.queryset = field.queryset.filter(is_present=True, is_deleted=False, link__isnull=True)


class ErspLinkInline(NoRelatedLinks, admin.TabularInline):
    """Блок «Связь с ЕРСП (agencyId)»: fk_name - department или agent."""

    model = ErspLink
    form = ErspLinkForm
    extra = 0
    fields = ["ersp_agency", "environment", "is_active"]
    readonly_fields = ["environment"]
    # «Добавить еще один agencyId» - подпись кнопки Django собирает из verbose_name.
    verbose_name = "agencyId"
    verbose_name_plural = "Связь с ЕРСП (agencyId)"
    classes = ["ersp-links"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "ersp_agency":
            # Справочник ЕРСП своей страховой (загружается командой load_ersp_agencies).
            kwargs["queryset"] = own_rows(request, ErspAgency.objects.all()).order_by("environment", "ersp_id")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_delete_permission(self, request, obj=None):
        return False


class DepartmentErspLinkInline(ErspLinkInline):
    fk_name = "department"


class AgentErspLinkInline(ErspLinkInline):
    fk_name = "agent"


# --- Соглашения и места -----------------------------------------------------------


class AgreementInline(NoRelatedLinks, admin.StackedInline):
    model = Agreement
    extra = 0
    fields = [
        ("number", "signed_on"),
        ("valid_from", "valid_to"),
        "signed_by",
        ("signer_name", "signer_position"),
        "signer_basis",
        ("power_of_attorney_number", "power_of_attorney_date"),
        ("terminated_on", "termination_reason"),
    ]
    readonly_fields = ["terminated_on", "termination_reason"]
    verbose_name = "соглашение"
    verbose_name_plural = "Соглашения"
    classes = ["agreements"]

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        signer = formset.form.base_fields.get("signed_by")
        if signer is not None:
            company_id = obj.insurance_company_id if obj is not None else request.user.insurance_company_id
            # Подписант - сотрудник своей страховой (штатный или ГПХ). Работал ли
            # он на дату заключения - проверяет соглашение (clean).
            signer.queryset = User.objects.filter(
                insurance_company_id=company_id, kind__in=[User.Kind.EMPLOYEE, User.Kind.CONTRACTOR],
            ).order_by("last_name", "first_name")
            signer.label_from_instance = lambda user: user.full_name()
            signer.help_text = (
                "Сотрудник своей страховой, работавший на дату заключения. Если его в системе нет "
                "(старое соглашение, бывший директор) - оставьте пустым и впишите ФИО и должность. "
                "«Действует по» - включительно, пусто - бессрочно. Досрочное прекращение - "
                "кнопкой «Прекратить соглашение» вверху карточки."
            )
        return formset

    def has_delete_permission(self, request, obj=None):
        return False


class PlacementInline(admin.TabularInline):
    model = Placement
    extra = 0
    fields = ["department_name", "since", "until", "basis"]
    readonly_fields = fields
    verbose_name = "место"
    verbose_name_plural = "Места (подразделение с даты по дату) - меняются кнопками «Прикрепить» и «Перевести»"

    @admin.display(description="подразделение")
    def department_name(self, placement):
        return placement.department.full_name()

    # Даты - как во всей системе (14.04.2021), а не «14 апреля 2021 г.».
    @admin.display(description="действует с")
    def since(self, placement):
        return f"{placement.valid_from:%d.%m.%Y}"

    @admin.display(description="действует по")
    def until(self, placement):
        return f"{placement.valid_to:%d.%m.%Y}" if placement.valid_to else "по сей день"

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class MemberInline(admin.TabularInline):
    """Пользователи агента: заводятся кнопкой «Завести пользователя», правится только «директор»."""

    model = Member
    extra = 0
    fields = ["person", "username", "is_director", "since", "until", "status", "actions"]
    readonly_fields = ["person", "username", "since", "until", "status", "actions"]
    verbose_name = "пользователь агента"
    verbose_name_plural = "Пользователи агента - входят каждый под своим логином"

    @admin.display(description="ФИО")
    def person(self, member):
        return member.user.full_name()

    @admin.display(description="логин")
    def username(self, member):
        return member.user.get_username()

    @admin.display(description="с")
    def since(self, member):
        return f"{member.valid_from:%d.%m.%Y}"

    @admin.display(description="по")
    def until(self, member):
        return f"{member.valid_to:%d.%m.%Y}" if member.valid_to else "по сей день"

    @admin.display(description="учётная запись")
    def status(self, member):
        return "работает" if member.user.is_active else "заблокирована"

    @admin.display(description="действия")
    def actions(self, member):
        links = [(reverse("admin:agents_agent_member_password", args=[member.agent_id, member.pk]), "Сбросить пароль")]
        if member.valid_to is None or member.valid_to >= _today():
            links.append((reverse("admin:agents_agent_member_disable", args=[member.agent_id, member.pk]), "Отключить"))
        return format_html_join(" · ", '<a href="{}">{}</a>', links)

    def has_add_permission(self, request, obj=None):
        return False  # кнопка «Завести пользователя»: там выдаётся временный пароль

    def has_delete_permission(self, request, obj=None):
        return False


# --- Агент ------------------------------------------------------------------------


class AgentForm(forms.ModelForm):
    class Meta:
        model = Agent
        fields = "__all__"


class AgentAddForm(AgentForm):
    """Новый агент - сразу с местом: подразделение с даты."""

    department = forms.ModelChoiceField(
        label="Подразделение", queryset=Department.objects.none(), empty_label="—",
        help_text="К какому подразделению прикреплён агент. Отдел - тоже можно: в ЕРСП уйдёт его головной офис или филиал.",
    )
    place_from = forms.DateField(label="Прикреплён с", initial=_today, widget=AdminDateWidget)
    place_basis = forms.CharField(label="Основание", required=False, max_length=255)


class WorkingFilter(admin.SimpleListFilter):
    title = "работает"
    parameter_name = "working"

    def lookups(self, request, model_admin):
        return [("yes", "да"), ("no", "нет")]

    def queryset(self, request, queryset):
        if self.value() not in ("yes", "no"):
            return queryset
        working = Agreement.objects.on().values("agent_id")
        return queryset.filter(pk__in=working) if self.value() == "yes" else queryset.exclude(pk__in=working)


@admin.register(Agent)
class AgentAdmin(NoRelatedLinks, TrackedAdmin):
    list_display = ["__str__", "kind", "working", "place_now", "agency_ids"]
    list_filter = [WorkingFilter, "kind", "is_electronic"]
    search_fields = ["counterparty__display_name", "counterparty__legal_entity__inn"]
    list_select_related = ["counterparty"]

    ADD_FIELDS = ["counterparty", "kind", "department", "place_from", "place_basis",
                  "area_type", "formation_date", ("is_electronic", "is_head_electronic"), "head_agent"]
    CHANGE_FIELDS = ["counterparty", "kind", "area_type", "formation_date",
                     ("is_electronic", "is_head_electronic"), "head_agent", "commission_account"]

    class Media:
        css = {"all": ["agents/agents.css"]}

    def get_queryset(self, request):
        return own_rows(request, super().get_queryset(request))

    # --- список ---

    @admin.display(description="работает сегодня", boolean=True)
    def working(self, agent):
        return agent.is_working()

    @admin.display(description="подразделение сейчас")
    def place_now(self, agent):
        placement = agent.current_placement()
        return placement.department.full_name() if placement else "—"

    @admin.display(description="agencyId")
    def agency_ids(self, agent):
        links = agent.ersp_links.filter(is_active=True).select_related("ersp_agency")
        return ", ".join(f"{link.ersp_agency.ersp_id} ({link.get_environment_display()})" for link in links) or "—"

    # --- карточка ---

    def get_fields(self, request, obj=None):
        return self.ADD_FIELDS if obj is None else self.CHANGE_FIELDS

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is None:
            name, options = fieldsets[0]
            fieldsets[0] = (name, {**options, "description": (
                "Агент - организация из справочника контрагентов своей страховой (сейчас только юрлица). "
                "Соглашение заводится в карточке после сохранения."
            )})
        name, options = fieldsets[0]
        fieldsets[0] = (name, {**options, "description": " ".join(filter(None, [
            options.get("description"),
            "Электронный агент - онлайн-платформа юрлица; у электронного агента, не головного, "
            "укажите его головного электронного.",
        ]))})
        return fieldsets

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            readonly.append("counterparty")  # контрагент агента не меняется (проверяет и база)
        return readonly

    def get_inlines(self, request, obj):
        if obj is None:
            return []
        return [AgreementInline, PlacementInline, MemberInline, AgentErspLinkInline]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        company_id = request.user.insurance_company_id
        if db_field.name == "counterparty":
            # Действующие юрлица своей страховой, ещё не заведённые агентами.
            kwargs["queryset"] = own_rows(request, Counterparty.objects.filter(
                kind=Counterparty.Kind.LEGAL_ENTITY, status=Counterparty.Status.ACTIVE,
            )).exclude(agents__isnull=False).order_by("display_name")
        elif db_field.name == "head_agent":
            kwargs["queryset"] = own_rows(request, Agent.objects.filter(is_head_electronic=True))
        elif db_field.name == "commission_account":
            kwargs["queryset"] = BankAccount.objects.none()  # своё у каждого агента - в get_form
        elif db_field.name == "area_type":
            kwargs["queryset"] = db_field.related_model.objects.filter(is_present=True)
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name == "counterparty" and company_id is None and field is not None:
            field.label_from_instance = lambda c: f"{c.insurance_company}: {c}"
        return field

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial.setdefault("area_type", 1)  # городская местность - чаще всего
        return initial

    def _prepare_form(self, request, form, obj):
        """Списки выбора, которые зависят от страховой и агента (и при отправке формы)."""
        company_id = request.user.insurance_company_id
        if "department" in form.fields:
            form.fields["department"].queryset = department_choices(company_id) if company_id else (
                Department.objects.filter(status=Department.Status.ACTIVE).select_related("parent")
            )
            form.fields["department"].label_from_instance = lambda d: d.full_name()
        if obj is not None and "commission_account" in form.fields:
            form.fields["commission_account"].queryset = BankAccount.objects.filter(
                counterparty_id=obj.counterparty_id, is_closed=False,
            )

    def get_form(self, request, obj=None, change=False, **kwargs):
        if obj is None:
            kwargs["form"] = AgentAddForm
        form_class = super().get_form(request, obj, change=change, **kwargs)
        admin_site = self
        company_id = request.user.insurance_company_id

        class Prepared(form_class):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                admin_site._prepare_form(request, self, obj)
                if obj is None and company_id:
                    # Новый агент сразу получает страховую сотрудника - до проверки формы.
                    self.instance.insurance_company_id = company_id

            def clean(self):
                data = super().clean()
                department = data.get("department")
                counterparty = data.get("counterparty")
                if department is not None and counterparty is not None and (
                    department.insurance_company_id != counterparty.insurance_company_id
                ):
                    self.add_error("department", "Это подразделение другой страховой.")
                return data

        return Prepared

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            if not change:
                data = form.cleaned_data
                services.place(
                    obj, department=data["department"], valid_from=data["place_from"],
                    basis=data.get("place_basis", ""), by=request.user,
                )

    def response_add(self, request, obj, post_url_continue=None):
        # После создания - сразу в карточку: там заводится соглашение.
        messages.info(request, "Заведите соглашение с агентом в блоке «Соглашения».")
        return redirect("admin:agents_agent_change", obj.pk)

    # --- кнопки-действия ---

    def get_urls(self):
        own = [
            path("<path:object_id>/place/", self.admin_site.admin_view(self.place_view), name="agents_agent_place"),
            path("<path:object_id>/transfer/", self.admin_site.admin_view(self.transfer_view),
                 name="agents_agent_transfer"),
            path("<path:object_id>/terminate/", self.admin_site.admin_view(self.terminate_view),
                 name="agents_agent_terminate"),
            path("<path:object_id>/add-member/", self.admin_site.admin_view(self.add_member_view),
                 name="agents_agent_add_member"),
            path("<path:object_id>/members/<int:member_id>/disable/", self.admin_site.admin_view(self.disable_member_view),
                 name="agents_agent_member_disable"),
            path("<path:object_id>/members/<int:member_id>/password/", self.admin_site.admin_view(self.member_password_view),
                 name="agents_agent_member_password"),
        ]
        return own + super().get_urls()

    def agent_actions(self, request, agent):
        if agent is None or not self.has_change_permission(request, agent):
            return []
        actions = []
        today = _today()
        if agent.current_placement() is not None:
            actions.append(("agents_agent_transfer", "Перевести"))
        elif not agent.placements.filter(valid_from__gt=today).exists():
            actions.append(("agents_agent_place", "Прикрепить"))
        agreement = agent.current_agreement()
        if agreement is not None and agreement.terminated_on is None:
            actions.append(("agents_agent_terminate", "Прекратить соглашение"))
        if request.user.has_perm("agents.add_member"):
            actions.append(("agents_agent_add_member", "Завести пользователя"))
        return [{"url": reverse(f"admin:{name}", args=[agent.pk]), "label": label} for name, label in actions]

    def change_view(self, request, object_id, form_url="", extra_context=None):
        agent = self.get_object(request, unquote(object_id))
        extra_context = {**(extra_context or {}), "agent_actions": self.agent_actions(request, agent)}
        return super().change_view(request, object_id, form_url, extra_context)

    def _target(self, request, object_id):
        # Сначала права, потом поиск: тому, кому агенты не положены (платформа),
        # - однозначный отказ 403, а не «не найдено».
        if not self.has_change_permission(request):
            raise PermissionDenied
        agent = self.get_object(request, unquote(object_id))
        if agent is None:
            raise Http404("Агент не найден.")
        if not self.has_change_permission(request, agent):
            raise PermissionDenied
        return agent

    def _render_action(self, request, agent, form, title, submit_label, intro=()):
        placement = agent.current_placement()
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": f"{title}: {agent}",
            "target": agent,
            "form": form,
            "adminform": admin.helpers.AdminForm(form, [(None, {"fields": list(form.fields)})], {}),
            "media": self.media + form.media,
            "submit_label": submit_label,
            "intro": intro,
            "place": placement.department.full_name() if placement else "",
            "change_url": reverse("admin:agents_agent_change", args=[agent.pk]),
        }
        return TemplateResponse(request, "admin/agents/agent/action.html", context)

    @staticmethod
    def _errors_to_form(form, error):
        """Ошибки правил (ValidationError) - к полям формы, остальные - общей ошибкой."""
        if hasattr(error, "error_dict"):
            for field, errors in error.error_dict.items():
                form.add_error(field if field in form.fields else None, errors)
        else:
            form.add_error(None, error)

    def _run(self, request, agent, form, action, done):
        """Выполнить действие; ошибки правил - к полям формы."""
        try:
            action()
        except ValidationError as error:
            self._errors_to_form(form, error)
            return None
        messages.success(request, done)
        return redirect("admin:agents_agent_change", agent.pk)

    def place_view(self, request, object_id):
        agent = self._target(request, object_id)
        form = PlaceForm(request.POST or None)
        form.fields["department"].queryset = department_choices(agent.insurance_company_id)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            done = self._run(request, agent, form, lambda: services.place(
                agent, department=data["department"], valid_from=data["valid_from"],
                basis=data["basis"], by=request.user,
            ), f"{agent}: прикреплён к «{data['department'].full_name()}» с {data['valid_from']:%d.%m.%Y}.")
            if done:
                return done
        return self._render_action(request, agent, form, "Прикрепить", "Прикрепить", [
            "Агент прикрепляется к подразделению с указанной даты.",
        ])

    def transfer_view(self, request, object_id):
        agent = self._target(request, object_id)
        form = PlaceForm(request.POST or None, label_from="Дата перевода")
        form.fields["department"].queryset = department_choices(agent.insurance_company_id)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            done = self._run(request, agent, form, lambda: services.transfer(
                agent, department=data["department"], valid_from=data["valid_from"],
                basis=data["basis"], by=request.user,
            ), f"{agent}: переведён в «{data['department'].full_name()}» с {data['valid_from']:%d.%m.%Y}.")
            if done:
                return done
        return self._render_action(request, agent, form, "Перевести", "Перевести", [
            "Нынешнее место закроется накануне даты перевода, новое начнётся с неё.",
            "Договоры, заключённые до перевода, остаются за прежним подразделением.",
        ])

    def terminate_view(self, request, object_id):
        agent = self._target(request, object_id)
        agreement = agent.current_agreement()
        if agreement is None or agreement.terminated_on is not None:
            messages.warning(request, "У агента нет действующего соглашения.")
            return redirect("admin:agents_agent_change", agent.pk)
        form = TerminateForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            data = form.cleaned_data
            done = self._run(request, agent, form, lambda: services.terminate(
                agreement, last_day=data["last_day"], reason=data["reason"], by=request.user,
            ), f"{agent}: соглашение {agreement} прекращено, последний день - {data['last_day']:%d.%m.%Y}.")
            if done:
                return done
        agency_ids = ", ".join(
            f"{link.ersp_agency.ersp_id} ({link.get_environment_display()})"
            for link in agent.ersp_links.filter(is_active=True).select_related("ersp_agency")
        )
        return self._render_action(request, agent, form, "Прекратить соглашение", "Прекратить", [
            f"Соглашение {agreement}, действует с {agreement.valid_from:%d.%m.%Y}"
            + (f" по {agreement.valid_to:%d.%m.%Y}." if agreement.valid_to else ", бессрочно."),
            "Со следующего дня после последнего агент не работает: новые продажи от его имени не принимаются.",
            "В ЕРСП прекращение не уходит само: удалите агента кнопкой «Delete» в веб-интерфейсе ЕРСП"
            + (f" (agencyId {agency_ids})." if agency_ids else "."),
        ])


    # --- пользователи агента ---

    def _member(self, request, agent, member_id):
        if not request.user.has_perm("agents.change_member"):
            raise PermissionDenied
        member = agent.members.select_related("user").filter(pk=member_id).first()
        if member is None:
            raise Http404("Пользователь агента не найден.")
        return member

    def _render_password(self, request, agent, user, password):
        context = {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": "Временный пароль",
            "target": agent,
            "person": user,
            "password": password,
            "change_url": reverse("admin:agents_agent_change", args=[agent.pk]),
        }
        response = TemplateResponse(request, "admin/agents/agent/password_issued.html", context)
        response["Cache-Control"] = "no-store"
        return response

    def add_member_view(self, request, object_id):
        agent = self._target(request, object_id)
        if not request.user.has_perm("agents.add_member"):
            raise PermissionDenied
        form = MemberAddForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            try:
                member, password = services.add_member(agent, by=request.user, **form.cleaned_data)
            except ValidationError as error:
                self._errors_to_form(form, error)
            else:
                user = member.user
                messages.success(request, f"{agent}: заведён пользователь {user.full_name()} (логин {user.get_username()}).")
                return self._render_password(request, agent, user, password)
        return self._render_action(request, agent, form, "Завести пользователя", "Завести", [
            "Учётная запись сотрудника агентства: каждый входит под своим логином. Директор агента видит "
            "все продажи агентства, остальные - только свои.",
            "Пароль придумает система и покажет один раз; при первом входе человек задаст свой.",
            "Войти он сможет, пока у агента действует соглашение (кабинет агента появится вместе с договорами).",
        ])

    def disable_member_view(self, request, object_id, member_id):
        agent = self._target(request, object_id)
        member = self._member(request, agent, member_id)
        form = DisableForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            day = form.cleaned_data["last_day"]
            done = self._run(request, agent, form, lambda: services.disable_member(member, last_day=day, by=request.user),
                             f"{member.user.full_name()}: отключён(а), последний день - {day:%d.%m.%Y}.")
            if done:
                return done
        return self._render_action(request, agent, form, f"Отключить {member.user.full_name()}", "Отключить", [
            "Связь с агентом закроется последним днём. Если он сегодня или прошёл - учётная запись блокируется "
            "сразу; если впереди - вход закроется сам на следующий день.",
            "Учётная запись не удаляется: продажи остаются за человеком.",
        ])

    def member_password_view(self, request, object_id, member_id):
        agent = self._target(request, object_id)
        member = self._member(request, agent, member_id)
        if request.method == "POST":
            from accounts import personnel

            password = personnel.issue_temporary_password(member.user, by=request.user)
            return self._render_password(request, agent, member.user, password)
        return self._render_action(request, agent, forms.Form(), f"Сбросить пароль: {member.user.full_name()}",
                                   "Выдать временный пароль", [
            "Система придумает временный пароль и покажет его один раз. Нынешний пароль человека перестанет "
            "действовать; при входе он задаст свой.",
        ])


class MemberAddForm(forms.Form):
    username = forms.CharField(label="Логин", max_length=150)
    last_name = forms.CharField(label="Фамилия", max_length=150)
    first_name = forms.CharField(label="Имя", max_length=150)
    patronymic = forms.CharField(label="Отчество", max_length=150, required=False)
    # Длину телефона по цифрам проверяет модель: здесь «+998 (90) 123-45-67» не режем (ловушка ввода, AGENTS.md).
    phone = forms.CharField(label="Телефон", max_length=40, required=False, help_text="С кодом страны, например 998901234567.")
    email = forms.EmailField(label="Эл. почта", required=False)
    is_director = forms.BooleanField(label="Директор агента", required=False,
                                     help_text="Видит все продажи агентства.")
    valid_from = forms.DateField(label="Работает с", initial=_today, widget=AdminDateWidget)


class DisableForm(forms.Form):
    last_day = forms.DateField(
        label="Последний день", initial=_today, widget=AdminDateWidget,
        help_text="Включительно. Сегодня или раньше - учётная запись блокируется сразу.",
    )


class PlaceForm(forms.Form):
    department = forms.ModelChoiceField(label="Подразделение", queryset=Department.objects.none(), empty_label="—")
    valid_from = forms.DateField(label="С даты", initial=_today, widget=AdminDateWidget)
    basis = forms.CharField(label="Основание", required=False, max_length=255)

    def __init__(self, *args, label_from=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].label_from_instance = lambda d: d.full_name()
        if label_from:
            self.fields["valid_from"].label = label_from
            self.fields["valid_from"].help_text = "Первый день в новом подразделении. Нынешнее место закроется накануне."


class TerminateForm(forms.Form):
    last_day = forms.DateField(
        label="Последний день", initial=_today, widget=AdminDateWidget,
        help_text="Включительно. Со следующего дня агент не работает.",
    )
    reason = forms.CharField(label="Причина", max_length=500, widget=forms.Textarea(attrs={"rows": 3}))
