"""
Справочник контрагентов в админ-панели.

Банки, основания полномочий, должности и карточки контрагентов (с данными
юрлица внутри карточки). Удаление запрещено всем, даже суперпользователю
(см. audit.admin.TrackedAdmin): вместо него - архив или «не действует».

Мультиарендность: сотрудник видит и заводит контрагентов только своей
страховой компании; страховая подставляется из его учётной записи.
Пользователь без страховой компании (администратор системы) видит всех
и выбирает страховую при создании.
"""
import re

from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.forms.models import BaseInlineFormSet
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from audit.admin import TrackedAdmin
from audit.models import Source
from integrations.ersp import PROVIDER, ErspClient, ErspError
from integrations.models import Exchange
from references.models import CbuBank

from . import banks, provider

from .models import (
    Bank,
    BankAccount,
    Counterparty,
    LegalEntity,
    PowerBasis,
    Position,
    Representative,
)
from .ersp import Role, ersp_gaps
from .normalize import remove_spaces, search_key


@admin.register(Bank)
class BankAdmin(TrackedAdmin):
    """
    Справочник банков. Рядом - данные перечня ЦБ по этому МФО
    (references.CbuBank); при сохранении пустой ИНН подставляется
    из ЦБ (counterparties/banks.py), отдельной записью в журнале.
    """

    list_display = ["mfo", "name", "inn", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["mfo", "name", "inn"]
    fields = ["mfo", "name", "inn", "is_active", "cbu_info"]
    readonly_fields = ["cbu_info"]

    @admin.display(description="по данным ЦБ")
    def cbu_info(self, bank):
        if not bank.pk:
            return "после сохранения пустой ИНН подставится из перечня ЦБ, если МФО там есть"
        record = CbuBank.objects.filter(mfo=bank.mfo).first()
        if record is None:
            if not CbuBank.objects.exists():
                return "перечень ЦБ ещё не загружен (команда load_cbu_banks)"
            return "МФО нет в перечне ЦБ"
        parts = [record.branch_name, f"ИНН {record.inn}" if record.inn else "ИНН не указан",
                 record.get_kind_display()]
        if record.changed_on:
            parts.append(f"изменено в ЦБ {record.changed_on:%d.%m.%Y}")
        text = "; ".join(parts)
        if not record.is_present:
            text += ". В последнем перечне ЦБ этого МФО нет - возможно, филиал закрыт."
        if bank.inn and record.inn and bank.inn != record.inn:
            text += ". ИНН в ЦБ отличается от нашего - проверьте."
        return text

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        record = banks.cbu_record(obj.mfo)
        changed = banks.fill_from_cbu(obj, record) if record is not None else []
        if changed:
            obj.save(by=request.user, source=Source.CBU)
            what = {"inn": f"ИНН {obj.inn}", "name": f"название «{obj.name}»"}
            messages.info(request, "Из перечня ЦБ подставлено: " + ", ".join(what[f] for f in changed) + ".")


class InputChoiceAdmin(TrackedAdmin):
    list_display = ["name", "is_other", "sort_order", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name"]
    fields = ["name", "is_other", "sort_order", "is_active"]
    readonly_fields = ["is_other"]  # «Иное» заводится только миграцией


@admin.register(PowerBasis)
class PowerBasisAdmin(InputChoiceAdmin):
    pass


@admin.register(Position)
class PositionAdmin(InputChoiceAdmin):
    pass


# --- Контрагенты ------------------------------------------------------------------

class LegalEntityFormSet(BaseInlineFormSet):
    def _construct_form(self, i, **kwargs):
        # Передаём блоку карточку (возможно, ещё не сохранённую) - см.
        # LegalEntity.card(). Без этого проверки повтора ИНН и телефона
        # в форме не срабатывали, и повтор ИНН давал ошибку 500.
        form = super()._construct_form(i, **kwargs)
        form.instance.card_in_form = self.instance
        return form


class NoRelatedLinks:
    """
    Убирает значки ✎ + 👁 у выпадающих списков (должность, банк, валюта...).
    Справочники правятся в своих разделах админ-панели, а не мимоходом из
    карточки клиента; к тому же значки сбивали выравнивание полей.
    """

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        widget = getattr(formfield, "widget", None)
        if isinstance(widget, RelatedFieldWidgetWrapper):
            widget.can_add_related = False
            widget.can_change_related = False
            widget.can_view_related = False
            widget.can_delete_related = False
        return formfield


class CardChildFormSet(BaseInlineFormSet):
    """
    Проверки по всем строкам вложенного блока сразу: «основной - только один»
    и «номер не повторяется». В базе эти правила есть, но в них участвует
    ссылка на карточку, которой нет в форме, - Django их в форме не проверяет
    (ловушка из AGENTS.md), и без этой проверки была бы ошибка 500.
    """

    def clean(self):
        # Наши проверки - раньше встроенных Django: у встроенной проверки
        # повтора сообщение с техническим именем поля («number»).
        model = self.model
        rows = [
            form.instance
            for form in self.forms
            if getattr(form, "cleaned_data", None) and not form.cleaned_data.get("DELETE")
        ]
        if sum(1 for row in rows if row.is_primary) > 1:
            raise ValidationError(f"Основным может быть только один: {model._meta.verbose_name_plural}.")
        numbers = [row.number for row in rows if getattr(row, "number", "")]
        repeated = sorted({n for n in numbers if numbers.count(n) > 1})
        if repeated:
            raise ValidationError(f"Номер указан дважды: {', '.join(repeated)}.")
        super().clean()


class CardChildForm(forms.ModelForm):
    """
    Строка вложенного блока. Правило «основной - только один» проверяется не
    построчно, а по всему блоку (CardChildFormSet): построчная проверка смотрит
    в базу, где старый основной ещё основной, и не даёт сменить основного
    в одной форме. При сохранении сначала снимается старый основной
    (audit.admin.TrackedAdmin.save_formset), так что база правило не нарушит.
    """

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        exclude.add("is_primary")
        return exclude


class LegalEntityInline(NoRelatedLinks, admin.StackedInline):
    """Данные юрлица - блок внутри карточки контрагента."""

    model = LegalEntity
    formset = LegalEntityFormSet
    can_delete = False
    min_num = 1
    max_num = 1
    extra = 0
    verbose_name = "данные юрлица"
    verbose_name_plural = "данные юрлица"
    # ОКЭД (696 кодов) и валюты (164) - поиском, а не длинным списком.
    autocomplete_fields = ["oked", "charter_capital_currency"]
    fieldsets = [
        (None, {"fields": ["is_foreign", "country", "inn", "full_name", "short_name"]}),
        ("Деятельность", {"fields": ["oked", "okpo", "ownership_form"]}),
        ("Регистрация", {"fields": [
            "reg_certificate_number", "reg_certificate_date",
            "charter_capital", "charter_capital_currency",
        ]}),
        ("Район по СОАТО", {"fields": ["soato_code", "soato_name"]}),
    ]


class RepresentativeInline(NoRelatedLinks, admin.StackedInline):
    """Представители организации - блок внутри карточки."""

    model = Representative
    form = CardChildForm
    formset = CardChildFormSet
    can_delete = False  # не удаляются: срок полномочий закрывается датой «действует по»
    extra = 0
    fieldsets = [
        (None, {"fields": [
            ("full_name", "is_primary"),
            ("position", "position_other"),
            ("power_basis", "power_basis_other", "power_basis_number"),
            ("pinfl", "passport_series", "passport_number"),
            ("valid_from", "valid_to"),
        ]}),
    ]


class BankAccountInline(NoRelatedLinks, admin.TabularInline):
    """Банковские счета - блок внутри карточки."""

    model = BankAccount
    form = CardChildForm
    formset = CardChildFormSet
    can_delete = False  # не удаляются: ставится отметка «закрыт»
    extra = 0
    fields = ["bank", "foreign_bank_name", "number", "currency", "is_primary", "is_closed"]
    autocomplete_fields = ["bank", "currency"]


@admin.register(Counterparty)
class CounterpartyAdmin(NoRelatedLinks, TrackedAdmin):
    inlines = [LegalEntityInline, RepresentativeInline, BankAccountInline]
    list_display = ["display_name", "inn", "is_foreign", "region", "status"]
    list_filter = ["status", "legal_entity__is_foreign", "region"]
    list_select_related = ["legal_entity", "region", "insurance_company"]
    # Поиск устроен по-своему (get_search_results): наименование - по ключу
    # для поиска, ИНН - точно. Строка ниже нужна, чтобы появилось поле поиска.
    search_fields = ["search_text"]
    search_help_text = "Наименование (любой частью, кириллицей или латиницей) или ИНН."

    actions = ["archive", "restore"]

    @admin.action(description="В архив", permissions=["change"])
    def archive(self, request, queryset):
        count = self._set_status(request, queryset, Counterparty.Status.ARCHIVED)
        self.message_user(request, f"Переведено в архив: {count}.")

    @admin.action(description="Вернуть из архива", permissions=["change"])
    def restore(self, request, queryset):
        count = self._set_status(request, queryset, Counterparty.Status.ACTIVE)
        self.message_user(request, f"Возвращено из архива: {count}.")

    @staticmethod
    def _set_status(request, queryset, status):
        # По одной записи и с сотрудником - чтобы каждая смена статуса
        # попала в журнал (массовое изменение журнал обходит и запрещено).
        count = 0
        for counterparty in queryset.exclude(status=status):
            counterparty.status = status
            counterparty.save(by=request.user, update_fields=["status"])
            count += 1
        return count

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        company_id = request.user.insurance_company_id
        if company_id:
            return qs.filter(insurance_company_id=company_id)
        if request.user.is_superuser:
            return qs  # администратор системы видит все страховые
        return qs.none()

    def get_fields(self, request, obj=None):
        fields = ["phone", "email", "region", "address", "status"]
        if obj is not None:
            fields = ["display_name", "ersp_readiness", *fields]
        if not request.user.insurance_company_id:
            fields = ["insurance_company", *fields]
        return fields

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            # Страховая задаётся один раз; наименование собирает система.
            readonly += ["insurance_company", "display_name", "ersp_readiness"]
        return readonly

    def get_list_display(self, request):
        if request.user.insurance_company_id:
            return self.list_display
        return [*self.list_display, "insurance_company"]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        company_id = request.user.insurance_company_id
        if obj is None and company_id:
            # Новая карточка сразу получает страховую сотрудника - ещё до
            # проверки формы, чтобы проверка повтора ИНН знала, где искать.
            class FormWithCompany(form):
                def __init__(self, *args, **kw):
                    super().__init__(*args, **kw)
                    self.instance.insurance_company_id = company_id

            return FormWithCompany
        return form

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Регион по СОАТО - после сохранения всей карточки: и когда изменили
        # СОАТО, и когда сотрудник очистил регион, не трогая данные юрлица.
        form.instance.fill_region_from_soato(by=request.user)

    def get_search_results(self, request, queryset, search_term):
        term = search_term.strip()
        if not term:
            return queryset, False
        found = queryset.filter(
            Q(search_text__icontains=search_key(term))
            | Q(legal_entity__inn=remove_spaces(term).upper())
        )
        return found, False

    @admin.display(description="готовность к ЕРСП")
    def ersp_readiness(self, obj):
        """
        Чего не хватает для отправки в ЕРСП - по сохранённой карточке
        (после правки нажмите «Сохранить и продолжить редактирование»).
        """
        blocks = []
        for role, title in [
            (Role.INSURANT, "Как страхователь"),
            (Role.OTHER, "Как выгодоприобретатель, застрахованный, владелец объекта"),
        ]:
            gaps = ersp_gaps(obj, role)
            if gaps:
                items = format_html_join("", "<li>{}</li>", ((gap,) for gap in gaps))
                blocks.append(format_html("<p><strong>{}</strong> - не хватает:</p><ul>{}</ul>", title, items))
            else:
                blocks.append(format_html("<p><strong>{}</strong> - готов ✓</p>", title))
        return format_html_join("", "{}", ((block,) for block in blocks))

    # --- Заполнение по ИНН от провайдера ЕРСП (counterparties/provider.py) ---------
    #
    # Запрос к ЕРСП - только по кнопке. Ответ ложится в журнал обмена, и
    # страница сверки дальше работает с этой строкой журнала (?exchange=<номер>):
    # сотрудник применяет ровно тот ответ, который видел, а обновление страницы
    # не шлёт в ЕРСП новый запрос.

    def get_urls(self):
        own = [
            path("by-inn/", self.admin_site.admin_view(self.by_inn_view),
                 name="counterparties_counterparty_by_inn"),
            path("<path:object_id>/provider/", self.admin_site.admin_view(self.provider_view),
                 name="counterparties_counterparty_provider"),
        ]
        return own + super().get_urls()  # свои - раньше: иначе by-inn/ примут за номер карточки

    def _ask_provider(self, request, company, inn):
        """Запрос к провайдеру. Возвращает строку журнала или None (ошибка уже показана)."""
        try:
            client = ErspClient.for_purpose(PROVIDER, company, user=request.user)
            client.provider_inn(inn)
        except ErspError as error:
            messages.error(request, f"Провайдер ЕРСП: {error.message}")
            return None
        return client.last_exchange

    @staticmethod
    def _stored_answer(exchange_id, company, inn=None):
        """Успешный ответ провайдера из журнала обмена - только своей страховой."""
        try:
            pk = int(exchange_id)
        except (TypeError, ValueError):
            return None
        found = Exchange.objects.filter(
            pk=pk, operation=Exchange.Operation.PROVIDER_INN,
            outcome=Exchange.Outcome.OK, insurance_company=company,
        )
        if inn is not None:
            found = found.filter(subject=inn)
        return found.first()

    def _page(self, request, title, context):
        return TemplateResponse(
            request,
            "admin/counterparties/counterparty/provider.html",
            {**self.admin_site.each_context(request), "opts": self.model._meta, "title": title, **context},
        )

    def provider_view(self, request, object_id):
        """Сверка карточки с провайдером: у нас | у провайдера | применить."""
        card = self.get_object(request, unquote(object_id))  # только своей страховой
        if card is None:
            return self._get_obj_does_not_exist_redirect(request, self.model._meta, object_id)
        if not self.has_change_permission(request, card):
            raise PermissionDenied
        change_url = reverse("admin:counterparties_counterparty_change", args=[card.pk])
        legal = getattr(card, "legal_entity", None)
        if legal is None or legal.is_foreign:
            messages.error(request, "Провайдер ЕРСП знает только организации Узбекистана.")
            return redirect(change_url)

        company = card.insurance_company
        exchange_id = request.POST.get("exchange") or request.GET.get("exchange")
        if not exchange_id:
            exchange = self._ask_provider(request, company, legal.inn)
            if exchange is None:
                return redirect(change_url)
            return redirect(f"{request.path}?exchange={exchange.pk}")

        exchange = self._stored_answer(exchange_id, company, inn=legal.inn)
        if exchange is None:
            messages.error(request, "Ответ провайдера не найден - запросите заново.")
            return redirect(change_url)
        data = provider.parse(legal.inn, exchange.response_body["result"])

        if request.method == "POST":
            try:
                done = provider.apply(card, data, request.POST.getlist("apply"), request.user)
            except ValidationError as error:
                messages.error(request, "Не применено: " + " ".join(error.messages))
                return redirect(f"{request.path}?exchange={exchange.pk}")
            if done:
                messages.success(request, "Применено от провайдера: " + "; ".join(done) + ".")
            else:
                messages.info(request, "Ничего не отмечено - карточка не изменилась.")
            return redirect(change_url)

        rows = provider.compare(card, data)
        return self._page(request, f"Сверка с провайдером ЕРСП: {card}", {
            "card": card, "inn": legal.inn, "exchange": exchange, "data": data,
            "rows": rows, "can_apply": any(row.selectable for row in rows), "change_url": change_url,
        })

    def by_inn_view(self, request):
        """Новый контрагент по ИНН: ввод ИНН -> ответ провайдера -> создать."""
        if not self.has_add_permission(request):
            raise PermissionDenied
        list_url = reverse("admin:counterparties_counterparty_changelist")
        company = request.user.insurance_company
        if company is None:
            messages.error(request, "Заведение по ИНН - для сотрудника страховой компании "
                                    "(в учётной записи не указана страховая).")
            return redirect(list_url)

        def existing_card(inn):
            legal = LegalEntity.objects.filter(
                insurance_company=company, inn=inn, is_foreign=False
            ).select_related("counterparty").first()
            return legal.counterparty if legal else None

        def go_to_existing(card):
            messages.info(request, f"Контрагент с этим ИНН уже есть: {card}. Вот сверка с провайдером.")
            if self.has_change_permission(request, card):
                return redirect(reverse("admin:counterparties_counterparty_provider", args=[card.pk]))
            return redirect(reverse("admin:counterparties_counterparty_change", args=[card.pk]))

        action = request.POST.get("action")
        if request.method == "POST" and action == "query":
            inn = re.sub(r"\s", "", request.POST.get("inn", ""))
            if not re.fullmatch(r"[0-9]{9}", inn):
                return self._page(request, "Новый контрагент по ИНН", {
                    "ask_inn": True, "inn": inn, "inn_error": "ИНН организации Узбекистана - ровно 9 цифр.",
                })
            card = existing_card(inn)
            if card is not None:
                return go_to_existing(card)
            exchange = self._ask_provider(request, company, inn)
            if exchange is None:
                return self._page(request, "Новый контрагент по ИНН", {"ask_inn": True, "inn": inn})
            return redirect(f"{request.path}?exchange={exchange.pk}")

        exchange_id = request.POST.get("exchange") or request.GET.get("exchange")
        if not exchange_id:
            return self._page(request, "Новый контрагент по ИНН", {"ask_inn": True})
        exchange = self._stored_answer(exchange_id, company)
        if exchange is None:
            messages.error(request, "Ответ провайдера не найден - запросите заново.")
            return redirect(request.path)
        inn = exchange.subject
        card = existing_card(inn)
        if card is not None:
            return go_to_existing(card)
        data = provider.parse(inn, exchange.response_body["result"])

        if request.method == "POST" and action == "create":
            try:
                card = provider.create(company, data, request.POST.getlist("apply"), request.user)
            except (provider.ProviderError, ValidationError) as error:
                text = error.messages if isinstance(error, ValidationError) else [str(error)]
                messages.error(request, "Карточка не заведена: " + " ".join(text))
                return redirect(f"{request.path}?exchange={exchange.pk}")
            messages.success(request, f"Заведён контрагент по данным провайдера: {card}.")
            return redirect(reverse("admin:counterparties_counterparty_change", args=[card.pk]))

        return self._page(request, f"Новый контрагент по ИНН {inn}", {
            "new": True, "inn": inn, "exchange": exchange, "data": data,
            "can_create": data.values["full_name"].usable,
            "rows": provider.compare(None, data), "change_url": list_url,
        })

    @admin.display(description="ИНН", ordering="legal_entity__inn")
    def inn(self, obj):
        legal_entity = getattr(obj, "legal_entity", None)
        return legal_entity.inn if legal_entity else "—"

    @admin.display(description="иностранная", boolean=True, ordering="legal_entity__is_foreign")
    def is_foreign(self, obj):
        legal_entity = getattr(obj, "legal_entity", None)
        return legal_entity.is_foreign if legal_entity else None
