"""
Справочник контрагентов.

Здесь:
- справочники для ввода: банки, основания полномочий, должности представителей;
- карточка контрагента (Counterparty) - общая часть для юрлиц и физлиц;
- данные юрлица (LegalEntity) - одна строка на карточку юрлица.
Представители и счета добавляются следующим шагом.
Полное описание модели - docs/counterparties-model.md.

Общие правила:
- записи не удаляются: на них будут ссылаться контрагенты и полисы;
  вместо удаления снимается отметка «действует»;
- кто и когда создал и изменил запись - поля из audit.TrackedModel;
- проверки формата продублированы в базе (CheckConstraint): неверное
  значение не попадёт в таблицу, даже если его запишут в обход формы.
"""
import re

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from audit.models import Source, TrackedModel
from companies.models import UZBEKISTAN, InsuranceCompany
from references.models import ErspOwnershipForm, ErspRegion

from .normalize import digits_only, remove_spaces, search_key, squash_spaces

# --- Банки --------------------------------------------------------------------

MFO_REGEX = r"^[0-9]{5}$"
INN_REGEX = r"^[0-9]{9}$"


class Bank(TrackedModel):
    """
    Банк Узбекистана. Ключ - МФО (код банка, 5 цифр).

    ИНН банка провайдер ЕРСП не отдаёт: его вводят вручную один раз на банк,
    дальше он подставляется во все счета этого банка. ИНН не уникален:
    у разных МФО (филиалов) одного банка ИНН может совпадать.

    Иностранные банки в этот справочник не заносятся: у счёта в иностранном
    банке название банка пишется текстом.
    """

    mfo = models.CharField(
        "МФО",
        max_length=5,
        unique=True,
        validators=[RegexValidator(MFO_REGEX, "МФО - ровно 5 цифр, например 01095.")],
    )
    name = models.CharField("название", max_length=500)
    inn = models.CharField(
        "ИНН банка",
        max_length=9,
        blank=True,
        validators=[RegexValidator(INN_REGEX, "ИНН банка - ровно 9 цифр.")],
        help_text="Провайдер ЕРСП его не отдаёт; нужен для отправки счёта местного клиента в ЕРСП.",
    )
    is_active = models.BooleanField("действует", default=True)

    class Meta:
        verbose_name = "банк"
        verbose_name_plural = "банки"
        ordering = ["mfo"]
        constraints = [
            models.CheckConstraint(
                condition=Q(mfo__regex=MFO_REGEX),
                name="counterparties_bank_mfo_5_digits",
                violation_error_message="МФО - ровно 5 цифр.",
            ),
            models.CheckConstraint(
                condition=Q(inn="") | Q(inn__regex=INN_REGEX),
                name="counterparties_bank_inn_9_digits_or_empty",
                violation_error_message="ИНН банка - ровно 9 цифр или пусто.",
            ),
        ]

    def __str__(self):
        return f"{self.mfo} - {self.name}"

    def normalize(self):
        self.mfo = remove_spaces(self.mfo)
        self.inn = remove_spaces(self.inn)
        self.name = squash_spaces(self.name)


# --- Справочники для ввода представителя --------------------------------------

class InputChoice(TrackedModel):
    """
    Справочник для удобства ввода (в ЕРСП это просто текст).

    В каждом справочнике ровно один пункт «Иное» (is_other): если выбран он,
    сотрудник пишет значение своими словами. «Иное» нельзя выключить,
    иначе нестандартный случай станет невозможно ввести.
    """

    name = models.CharField("название", max_length=255)
    is_other = models.BooleanField(
        "пункт «Иное»",
        default=False,
        editable=False,
        help_text="При выборе этого пункта значение вводится текстом.",
    )
    sort_order = models.PositiveSmallIntegerField(
        "порядок в списке",
        default=100,
        help_text="Меньше - выше в списке. Одинаковые - по алфавиту.",
    )
    is_active = models.BooleanField("действует", default=True)

    class Meta:
        abstract = True
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="%(app_label)s_%(class)s_name_unique",
                violation_error_message="Такой пункт уже есть.",
            ),
            models.UniqueConstraint(
                fields=["is_other"],
                condition=Q(is_other=True),
                name="%(app_label)s_%(class)s_one_other",
                violation_error_message="Пункт «Иное» может быть только один.",
            ),
            models.CheckConstraint(
                condition=Q(is_other=False) | Q(is_active=True),
                name="%(app_label)s_%(class)s_other_always_active",
                violation_error_message="Пункт «Иное» нельзя выключить.",
            ),
        ]

    def __str__(self):
        return self.name

    def normalize(self):
        self.name = squash_spaces(self.name)

    def clean(self):
        # Правило «Иное нельзя выключить» уже есть в базе (CheckConstraint выше),
        # но в админ-панели поле «пункт «Иное»» только для чтения, а Django
        # не проверяет в форме ограничения, в которых участвует нередактируемое
        # поле. Без этой проверки сотрудник получал ошибку 500 вместо сообщения.
        if self.is_other and not self.is_active:
            raise ValidationError({"is_active": "Пункт «Иное» нельзя выключить."})


class PowerBasis(InputChoice):
    """Основание полномочий представителя (в ЕРСП - representativeDoc)."""

    class Meta(InputChoice.Meta):
        verbose_name = "основание полномочий"
        verbose_name_plural = "основания полномочий"


class Position(InputChoice):
    """Должность представителя (в ЕРСП - position)."""

    class Meta(InputChoice.Meta):
        verbose_name = "должность"
        verbose_name_plural = "должности"


# --- Карточка контрагента -------------------------------------------------------

PHONE_REGEX = r"^[0-9]{7,15}$"            # международный формат: только цифры
LOCAL_PHONE_REGEX = r"^998[0-9]{9}$"      # Узбекистан: 998 и 9 цифр
FOREIGN_INN_REGEX = r"^[A-Z0-9]{9,20}$"   # иностранный ИНН: буквы и цифры, от 9
COUNTRY_REGEX = r"^[A-Z]{2}$"
CURRENCY_REGEX = r"^[A-Z]{3}$"


class Counterparty(TrackedModel):
    """
    Карточка контрагента: общее для юрлиц и физлиц.

    Контрагент принадлежит одной страховой компании; клиентов одной страховой
    другая не видит. Страховая задаётся при создании (из учётной записи
    сотрудника) и больше не меняется.

    Отображаемое имя и ключ для поиска заполняет система по данным юрлица
    (позже - физлица); сотрудник их не вводит, в журнал они не пишутся -
    в журнал пишутся сами наименования, из которых они собраны.
    """

    class Kind(models.TextChoices):
        LEGAL_ENTITY = "legal_entity", "юридическое лицо"
        INDIVIDUAL = "individual", "физическое лицо"

    class Status(models.TextChoices):
        ACTIVE = "active", "действует"
        ARCHIVED = "archived", "архив"

    insurance_company = models.ForeignKey(
        InsuranceCompany,
        on_delete=models.PROTECT,
        related_name="counterparties",
        verbose_name="страховая компания",
    )
    kind = models.CharField(
        "тип", max_length=16, choices=Kind.choices, default=Kind.LEGAL_ENTITY, editable=False
    )
    display_name = models.CharField("наименование", max_length=500, blank=True, editable=False)
    search_text = models.TextField("ключ для поиска", blank=True, editable=False)
    phone = models.CharField(
        "телефон",
        max_length=15,
        blank=True,
        validators=[RegexValidator(PHONE_REGEX, "Телефон - от 7 до 15 цифр с кодом страны, например 998901234567.")],
        help_text="С кодом страны, без «+». Пробелы, скобки и дефисы уберутся сами.",
    )
    email = models.EmailField("эл. почта", blank=True)
    region = models.ForeignKey(
        ErspRegion,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="регион",
        help_text="Если пусто, система подставит регион по коду СОАТО района.",
    )
    address = models.CharField("адрес", max_length=500, blank=True)
    status = models.CharField(
        "статус", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )

    audit_exclude = ("display_name", "search_text")

    class Meta:
        verbose_name = "контрагент"
        verbose_name_plural = "контрагенты"
        ordering = ["display_name"]
        constraints = [
            # Нужна для составной ссылки из данных юрлица: она гарантирует,
            # что страховая компания у карточки и у юрлица одна и та же.
            models.UniqueConstraint(
                fields=["id", "insurance_company"],
                name="counterparties_counterparty_id_company",
            ),
            models.CheckConstraint(
                condition=Q(phone="") | Q(phone__regex=PHONE_REGEX),
                name="counterparties_counterparty_phone_digits",
                violation_error_message="Телефон - от 7 до 15 цифр.",
            ),
        ]

    def __str__(self):
        return self.display_name or f"Контрагент № {self.pk}"

    def normalize(self):
        self.phone = digits_only(self.phone)
        self.email = (self.email or "").strip()
        self.address = squash_spaces(self.address)

    def refresh_from_legal_entity(self, legal_entity, by=None):
        """
        Обновить то, что система собирает из данных юрлица: отображаемое имя,
        ключ для поиска и (если пуст) регион по коду СОАТО района.
        """
        self.display_name = legal_entity.short_name or legal_entity.full_name
        self.search_text = search_key(
            legal_entity.full_name, legal_entity.short_name, legal_entity.inn
        )
        self.save(by=by, update_fields=["display_name", "search_text"])

        if self.region_id is None and legal_entity.soato_code:
            region = ErspRegion.objects.filter(soato_code=legal_entity.soato_code[:4]).first()
            if region is not None:
                self.region = region
                # Регион выбрала система, а не сотрудник: так и пишем в журнал.
                self.save(by=by, source=Source.SYSTEM, update_fields=["region"])


class LegalEntity(TrackedModel):
    """
    Данные юридического лица (одна строка на карточку).

    ИНН уникален в паре «страховая + ИНН + иностранная». Страховая компания
    хранится и здесь (копия из карточки, заполняется автоматически), чтобы это
    правило проверяла сама база; составная ссылка в базе (миграция 0003)
    не даёт копии разойтись с карточкой.
    """

    counterparty = models.OneToOneField(
        Counterparty,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="legal_entity",
        verbose_name="контрагент",
    )
    insurance_company = models.ForeignKey(
        InsuranceCompany,
        on_delete=models.PROTECT,
        editable=False,
        related_name="+",
        verbose_name="страховая компания",
    )
    is_foreign = models.BooleanField("иностранная", default=False)
    inn = models.CharField(
        "ИНН",
        max_length=20,
        help_text="Местная - ровно 9 цифр; иностранная - от 9 до 20 латинских букв и цифр.",
    )
    country = models.CharField(
        "страна регистрации",
        max_length=2,
        default=UZBEKISTAN,
        validators=[RegexValidator(COUNTRY_REGEX, "Код страны - две латинские буквы (ISO 3166-1), например KZ.")],
        help_text="Код страны по ISO 3166-1. У местной организации всегда UZ.",
    )
    full_name = models.CharField("полное наименование", max_length=500)
    short_name = models.CharField("краткое наименование", max_length=255, blank=True)
    oked = models.CharField(
        "ОКЭД",
        max_length=5,
        blank=True,
        validators=[RegexValidator(r"^[0-9]{5}$", "ОКЭД - ровно 5 цифр.")],
    )
    oked_title = models.CharField("вид деятельности", max_length=500, blank=True)
    okpo = models.CharField(
        "ОКПО",
        max_length=8,
        blank=True,
        validators=[RegexValidator(r"^[0-9]{8}$", "ОКПО - ровно 8 цифр.")],
    )
    soato_code = models.CharField(
        "код СОАТО района",
        max_length=7,
        blank=True,
        validators=[RegexValidator(r"^[0-9]{7}$", "Код СОАТО района - ровно 7 цифр.")],
    )
    soato_name = models.CharField("район", max_length=255, blank=True)
    reg_certificate_number = models.CharField(
        "номер свидетельства о регистрации",
        max_length=50,
        blank=True,
        help_text="Это не страховая лицензия.",
    )
    reg_certificate_date = models.DateField("дата свидетельства о регистрации", null=True, blank=True)
    charter_capital = models.DecimalField(
        "уставный капитал",
        max_digits=20,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    charter_capital_currency = models.CharField(
        "валюта капитала",
        max_length=3,
        default="UZS",
        validators=[RegexValidator(CURRENCY_REGEX, "Валюта - три латинские буквы (ISO 4217), например UZS.")],
        help_text="Код валюты ISO 4217. Провайдер ЕРСП отдаёт капитал в сумах (UZS).",
    )
    ownership_form = models.ForeignKey(
        ErspOwnershipForm,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="форма собственности",
        help_text="Провайдер не отдаёт; в ЕРСП нужна только для страхователя.",
    )

    class Meta:
        verbose_name = "данные юрлица"
        verbose_name_plural = "данные юрлиц"
        constraints = [
            models.UniqueConstraint(
                fields=["insurance_company", "inn", "is_foreign"],
                name="counterparties_legalentity_inn_unique",
                violation_error_message="Контрагент с таким ИНН уже есть.",
            ),
            models.CheckConstraint(
                condition=Q(is_foreign=True) | Q(inn__regex=INN_REGEX),
                name="counterparties_legalentity_local_inn",
                violation_error_message="ИНН местной организации - ровно 9 цифр.",
            ),
            models.CheckConstraint(
                condition=Q(is_foreign=False) | Q(inn__regex=FOREIGN_INN_REGEX),
                name="counterparties_legalentity_foreign_inn",
                violation_error_message="ИНН иностранной организации - от 9 до 20 латинских букв и цифр.",
            ),
            models.CheckConstraint(
                condition=(Q(is_foreign=False) & Q(country=UZBEKISTAN))
                | (Q(is_foreign=True) & ~Q(country=UZBEKISTAN) & Q(country__regex=COUNTRY_REGEX)),
                name="counterparties_legalentity_country",
                violation_error_message="Страна: у местной организации - UZ, у иностранной - код другой страны.",
            ),
            models.CheckConstraint(
                condition=Q(oked="") | Q(oked__regex=r"^[0-9]{5}$"),
                name="counterparties_legalentity_oked",
                violation_error_message="ОКЭД - ровно 5 цифр.",
            ),
            models.CheckConstraint(
                condition=Q(okpo="") | Q(okpo__regex=r"^[0-9]{8}$"),
                name="counterparties_legalentity_okpo",
                violation_error_message="ОКПО - ровно 8 цифр.",
            ),
            models.CheckConstraint(
                condition=Q(soato_code="") | Q(soato_code__regex=r"^[0-9]{7}$"),
                name="counterparties_legalentity_soato",
                violation_error_message="Код СОАТО района - ровно 7 цифр.",
            ),
            models.CheckConstraint(
                condition=Q(charter_capital__isnull=True) | Q(charter_capital__gte=0),
                name="counterparties_legalentity_capital_not_negative",
                violation_error_message="Уставный капитал не может быть отрицательным.",
            ),
            models.CheckConstraint(
                condition=Q(charter_capital_currency__regex=CURRENCY_REGEX),
                name="counterparties_legalentity_currency",
                violation_error_message="Валюта - три латинские буквы.",
            ),
        ]

    def __str__(self):
        return f"{self.inn} - {self.short_name or self.full_name}"

    def card(self):
        """
        Карточка, к которой относятся данные юрлица.

        В форме админ-панели новая карточка ещё не сохранена, и Django
        не привязывает к ней данные юрлица до сохранения (связь идёт через
        главный ключ). Поэтому формсет передаёт карточку отдельно, в
        card_in_form - иначе проверки телефона и повтора ИНН не знали бы,
        с какой карточкой и страховой они работают.
        """
        counterparty = getattr(self, "counterparty", None)
        if counterparty is not None:
            return counterparty
        return getattr(self, "card_in_form", None)

    def normalize(self):
        # Страховая компания - всегда та же, что у карточки.
        counterparty = self.card()
        if counterparty is not None:
            self.insurance_company_id = counterparty.insurance_company_id
        self.inn = remove_spaces(self.inn or "").upper()
        self.country = remove_spaces(self.country or "").upper()
        if not self.is_foreign:
            self.country = UZBEKISTAN
        self.full_name = squash_spaces(self.full_name)
        self.short_name = squash_spaces(self.short_name)
        self.oked = remove_spaces(self.oked)
        self.oked_title = squash_spaces(self.oked_title)
        self.okpo = remove_spaces(self.okpo)
        self.soato_code = remove_spaces(self.soato_code)
        self.soato_name = squash_spaces(self.soato_name)
        self.reg_certificate_number = squash_spaces(self.reg_certificate_number)
        self.charter_capital_currency = remove_spaces(self.charter_capital_currency or "").upper()

    def clean(self):
        errors = []
        counterparty = self.card()

        # Телефон хранится в карточке, а правило для местных организаций
        # зависит от признака «иностранная», который хранится здесь.
        if counterparty is not None and not self.is_foreign:
            phone = digits_only(counterparty.phone)
            if phone and not re.fullmatch(LOCAL_PHONE_REGEX, phone):
                errors.append("Телефон местной организации - 998 и 9 цифр, например 998901234567.")

        # Повтор ИНН. В базе есть то же правило, но в нём участвует страховая
        # компания, которой нет в форме, а такие правила Django в форме
        # не проверяет (см. «Ловушка» в AGENTS.md).
        company_id = counterparty.insurance_company_id if counterparty is not None else None
        inn = remove_spaces(self.inn or "").upper()
        if company_id and inn:
            same = (
                LegalEntity.objects.filter(
                    insurance_company_id=company_id, inn=inn, is_foreign=self.is_foreign
                )
                .exclude(pk=self.pk)
                .select_related("counterparty")
                .first()
            )
            if same is not None:
                raise ValidationError(
                    {"inn": f"Контрагент с таким ИНН уже есть: {same.counterparty}."}
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, by=None, **kwargs):
        # Страховую компанию из карточки подставляет normalize() внутри save().
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "insurance_company"}
        super().save(*args, by=by, **kwargs)
        self.counterparty.refresh_from_legal_entity(self, by=by)
