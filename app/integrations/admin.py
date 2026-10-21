"""
Интеграции в админ-панели.

Подключение к ЕРСП (ErspConnectionAdmin) - карточка страховой:
- по каждой среде (боевая, песочница) client_id и логин видны и правятся;
  client_secret и пароль только вводятся: на экране «задан ••••», поле
  ввода всегда пустое (пустое при сохранении - «не менять»), значение
  и шифр не попадают в страницу ни в каком виде;
- «Проверить подключение» - новый вход в ЕРСП с сохранёнными данными,
  итог сразу на экране и в журнале обмена;
- состояние токена: когда получен, до какого времени действует, счётчик
  (сам токен не показывается);
- среда отправки полисов меняется отдельной страницей с подтверждением;
- после смены данных для входа токен этой страховой и среды забывается.
Сотрудник со страховой сразу попадает в карточку своей страховой
(она создаётся при первом открытии); администратор системы без
страховой видит список всех страховых.

Журнал обмена (ExchangeAdmin): только просмотр, даже у суперпользователя.

Мультиарендность: сотрудник видит свою страховую компанию;
администратор системы (без страховой) - все.
"""
import datetime
import json
import time

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html

from accounts.visibility import visible
from audit.admin import TrackedAdmin
from companies.models import InsuranceCompany

from . import ersp
from .models import Environment, ErspAgency, ErspConnection, ErspCredentials, Exchange


def own_companies(request, queryset):
    """
    Мультиарендность: своя страховая; администратор системы - все; иначе - ничего.
    Таблицы интеграций видны всей страховой при любой видимости
    (accounts/visibility.py).
    """
    return visible(request, queryset)


def moment(seconds):
    """Секунды Unix -> «20.10.2026 23:14» по часовому поясу сотрудника."""
    value = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
    return date_format(timezone.localtime(value), "d.m.Y H:i")


def left(seconds):
    """Сколько осталось: «11 дн. 16 ч 05 мин»."""
    if seconds <= 0:
        return "срок истёк"
    minutes = int(seconds) // 60
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    return (f"{days} дн. " if days else "") + f"{hours} ч {minutes:02d} мин"


# --- Подключение к ЕРСП ---------------------------------------------------------------

class CredentialsForm(forms.ModelForm):
    """
    Блок среды. client_secret и пароль - поля ввода без значения
    (render_value=False): форма никогда не отдаёт их в браузер.
    Пусто - не менять; «стереть» - удалить значение.
    """

    client_secret = forms.CharField(
        label="client_secret", required=False, strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    clear_client_secret = forms.BooleanField(label="стереть client_secret", required=False)
    password = forms.CharField(
        label="пароль", required=False, strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    clear_password = forms.BooleanField(label="стереть пароль", required=False)

    class Meta:
        model = ErspCredentials
        fields = ["client_id", "username"]
        widgets = {"username": forms.TextInput(attrs={"autocomplete": "off"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ErspCredentials.SECRET_FIELDS:
            is_set = self.instance.pk is not None and self.instance.has_secret(name)
            self.fields[name].help_text = (
                "задан ••••. Введите новое значение, чтобы заменить; пусто - не менять."
                if is_set else "не задан."
            )

    def clean(self):
        cleaned = super().clean()
        for name in ErspCredentials.SECRET_FIELDS:
            if cleaned.get(name) and cleaned.get(f"clear_{name}"):
                self.add_error(f"clear_{name}", "Либо новое значение, либо «стереть» - не одновременно.")
        return cleaned

    def save(self, commit=True):
        credentials = super().save(commit=False)
        for name in ErspCredentials.SECRET_FIELDS:
            if self.cleaned_data.get(name):
                credentials.set_secret(name, self.cleaned_data[name])
            elif self.cleaned_data.get(f"clear_{name}"):
                credentials.set_secret(name, "")
        if commit:
            credentials.save()
        return credentials


class ErspCredentialsInline(admin.StackedInline):
    model = ErspCredentials
    form = CredentialsForm
    extra = 0
    max_num = len(Environment.values)
    can_delete = False
    verbose_name = "среда"
    verbose_name_plural = "данные для входа по средам"
    fields = [
        "environment", "client_id", "client_secret", "clear_client_secret",
        "username", "password", "clear_password", "token_state", "check_button",
    ]
    readonly_fields = ["environment", "token_state", "check_button"]

    def has_add_permission(self, request, obj=None):
        return False  # оба блока создаёт ErspConnection.for_company()

    @admin.display(description="токен")
    def token_state(self, credentials):
        status = ersp.token_status(credentials.connection.insurance_company, credentials.environment)
        if status is None:
            return "нет - будет получен при первом запросе к ЕРСП или по кнопке проверки"
        got = f"получен {moment(status['obtained_at'])}" if status.get("obtained_at") else "получен"
        expires_at = status.get("expires_at")
        if not expires_at:
            return f"{got}; срок ЕРСП не назвал"
        return format_html(
            '{}; действует до {} по словам ЕРСП - осталось <strong data-ersp-countdown="{}">{}</strong>',
            got, moment(expires_at), int(expires_at), left(expires_at - time.time()),
        )

    @admin.display(description="проверка")
    def check_button(self, credentials):
        url = reverse(
            "admin:integrations_erspconnection_check",
            args=[credentials.connection_id, credentials.environment],
        )
        # Кнопка отправляет форму карточки на адрес проверки (formaction):
        # проверяются данные, уже сохранённые в базе.
        return format_html(
            '<button type="submit" class="button" formaction="{}" formnovalidate>Проверить подключение</button>'
            '<div class="help">Вход в ЕРСП с сохранёнными данными. Новые значения сначала сохраните.</div>',
            url,
        )


@admin.register(ErspConnection)
class ErspConnectionAdmin(TrackedAdmin):
    list_display = ["insurance_company", "send_environment", "prod_state", "sandbox_state"]
    list_select_related = ["insurance_company"]
    fields = ["insurance_company", "send_environment"]
    readonly_fields = ["insurance_company", "send_environment"]
    inlines = [ErspCredentialsInline]

    class Media:
        js = ["integrations/ersp_countdown.js"]

    def get_queryset(self, request):
        return own_companies(request, super().get_queryset(request))

    def has_add_permission(self, request):
        return False  # карточку создаёт for_company() при первом открытии

    def get_urls(self):
        own = [
            path("<path:object_id>/check/<str:environment>/", self.admin_site.admin_view(self.check_view),
                 name="integrations_erspconnection_check"),
            path("<path:object_id>/send-environment/", self.admin_site.admin_view(self.send_environment_view),
                 name="integrations_erspconnection_send_environment"),
        ]
        return own + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        # Карточка создаётся при первом открытии экрана (если можно править):
        # сотруднику страховой - своя, администратору системы - у каждой работающей.
        user = request.user
        if self.has_change_permission(request):
            if user.insurance_company_id:
                connection = ErspConnection.for_company(user.insurance_company, by=user)
                return redirect(reverse("admin:integrations_erspconnection_change", args=[connection.pk]))
            if user.is_superuser:
                for company in InsuranceCompany.objects.filter(is_active=True, ersp_connection__isnull=True):
                    ErspConnection.for_company(company, by=user)
        return super().changelist_view(request, extra_context)

    def save_formset(self, request, form, formset, change):
        super().save_formset(request, form, formset, change)
        # Данные для входа сменились - токен, полученный по старым, забываем:
        # следующий запрос войдёт уже с новыми (и сразу покажет, если они неверны).
        for credentials, _changed in formset.changed_objects:
            ersp.forget_token(credentials.connection.insurance_company, credentials.environment)
            missing = credentials.missing()
            label = credentials.get_environment_display()
            if missing:
                messages.warning(request, f"{label} среда: не заполнено - {', '.join(missing)}.")
            else:
                messages.info(request, f"{label} среда: данные для входа сохранены. Проверьте подключение.")

    @admin.display(description="боевая")
    def prod_state(self, connection):
        return self._state(connection, Environment.PROD)

    @admin.display(description="песочница")
    def sandbox_state(self, connection):
        return self._state(connection, Environment.SANDBOX)

    @staticmethod
    def _state(connection, environment):
        credentials = connection.credentials.filter(environment=environment).first()
        if credentials is None:
            return "—"
        missing = credentials.missing()
        return "заполнено" if not missing else f"нет: {', '.join(missing)}"

    def _editable(self, request, object_id):
        """Карточка своей страховой, которую сотрудник может править (иначе - отказ)."""
        connection = self.get_object(request, unquote(object_id))
        if connection is None or not self.has_change_permission(request, connection):
            raise PermissionDenied
        return connection

    def check_view(self, request, object_id, environment):
        """«Проверить подключение»: новый вход в ЕРСП, итог - сообщением на карточке."""
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        connection = self._editable(request, object_id)
        if environment not in Environment.values:
            raise PermissionDenied
        change_url = reverse("admin:integrations_erspconnection_change", args=[connection.pk])
        label = Environment(environment).label
        try:
            client = ersp.ErspClient(environment, connection.insurance_company, user=request.user)
            info = client.token_info()
        except ersp.ErspError as error:
            messages.error(request, f"Подключение к ЕРСП ({label} среда) не работает: {error.message}")
            return redirect(change_url)
        expires_in = info.get("expires_in")
        lifetime = f", токен действует {left(expires_in)}" if expires_in else ""
        messages.success(request, f"Подключение к ЕРСП ({label} среда) работает: вход выполнен{lifetime}.")
        return redirect(change_url)

    def send_environment_view(self, request, object_id):
        """
        Смена среды отправки полисов. На боевую - только с отметкой
        подтверждения: полисы начнут уходить в настоящий реестр регулятора.
        Изменение - в журнал изменений карточки (кто, когда, было -> стало).
        """
        connection = self._editable(request, object_id)
        change_url = reverse("admin:integrations_erspconnection_change", args=[connection.pk])
        error = ""
        if request.method == "POST":
            new = request.POST.get("send_environment")
            if new not in Environment.values:
                error = "Выберите среду."
            elif new == connection.send_environment:
                messages.info(request, "Среда отправки не изменилась.")
                return redirect(change_url)
            elif new == Environment.PROD and request.POST.get("confirm") != "yes":
                error = "Чтобы отправлять полисы в боевую среду, отметьте подтверждение."
            else:
                connection.send_environment = new
                connection.save(by=request.user)
                messages.success(
                    request, f"Отправка полисов: теперь {connection.get_send_environment_display()} среда."
                )
                return redirect(change_url)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Среда отправки полисов",
            "connection": connection,
            "change_url": change_url,
            "environments": Environment.choices,
            "error": error,
        }
        return TemplateResponse(request, "admin/integrations/erspconnection/send_environment.html", context)


# --- Журнал обмена ----------------------------------------------------------------------


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
        return own_companies(request, super().get_queryset(request))

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


@admin.register(ErspAgency)
class ErspAgencyAdmin(admin.ModelAdmin):
    """
    Справочник ЕРСП «Агенты/филиалы» своей страховой: только просмотр, даже
    у суперпользователя. Меняется только загрузкой (load_ersp_agencies).
    Связь с нашими подразделениями (и позже агентами) - на их экранах.
    """

    list_display = ["ersp_id", "name", "agency_type", "parent_ersp_id", "environment", "linked_to", "marks"]
    list_filter = ["environment", "agency_type", "is_deleted", "is_present"]
    search_fields = ["=ersp_id", "name", "inn"]
    readonly_fields = ["linked_to", "raw_pretty"]
    exclude = ["raw"]

    def get_queryset(self, request):
        return own_companies(request, super().get_queryset(request)).select_related("link__department")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="связано с")
    def linked_to(self, agency):
        link = getattr(agency, "link", None)
        if link is None:
            return "—"
        target = link.target()
        return f"{target}" + ("" if link.is_active else " (связь снята)")

    @admin.display(description="отметки")
    def marks(self, agency):
        marks = [m for m, on in (("удалён", agency.is_deleted), ("нет в ЕРСП", not agency.is_present)) if on]
        return ", ".join(marks) or "—"

    @admin.display(description="ответ ЕРСП")
    def raw_pretty(self, agency):
        return format_html("<pre>{}</pre>", json.dumps(agency.raw, ensure_ascii=False, indent=2))
