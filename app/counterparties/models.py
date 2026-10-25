"""
Справочник контрагентов.

Здесь:
- справочники для ввода: банки, основания полномочий, должности представителей;
- карточка контрагента (Counterparty) - общая часть для юрлиц и физлиц;
- данные юрлица (LegalEntity) - одна строка на карточку юрлица.
- представители организации (Representative) и банковские счета (BankAccount)
  - вложенные записи карточки.
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
from django.utils import timezone

from audit.models import Source, TrackedModel
from companies.models import UZBEKISTAN, InsuranceCompany
from references.models import (
    ErspCountry,
    ErspCurrency,
    ErspOked,
    ErspOwnershipForm,
    ErspRegion,
    ErspResidentType,
)

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
    # У данных юрлица тот же номер записи, что у карточки: их изменения видны
    # в «Истории» карточки (audit.admin.CardFilter).
    journal_same_number = ("counterparties.LegalEntity", "counterparties.Individual")

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
        self.fill_region_from_soato(by=by, soato_code=legal_entity.soato_code)

    def refresh_from_individual(self, individual, by=None):
        """Наименование и ключ поиска физлица: «ФАМИЛИЯ ИМЯ ОТЧЕСТВО» (как отдаёт провайдер)."""
        name = " ".join(p for p in (individual.last_name, individual.first_name, individual.middle_name) if p)
        self.display_name = name
        self.search_text = search_key(name, individual.pinfl, individual.passport)
        self.save(by=by, update_fields=["display_name", "search_text"])

    def fill_region_from_soato(self, by=None, soato_code=None):
        """
        Если регион пуст - поставить его по первым 4 цифрам кода СОАТО района.

        Вызывается после каждого сохранения карточки и данных юрлица:
        и когда изменился СОАТО, и когда сотрудник очистил регион.
        soato_code не передан - берётся из данных юрлица в базе.
        """
        if self.region_id is not None:
            return
        if soato_code is None:
            soato_code = (
                LegalEntity.objects.filter(pk=self.pk).values_list("soato_code", flat=True).first()
            )
        if not soato_code:
            return
        region = ErspRegion.objects.filter(soato_code=soato_code[:4]).first()
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
    oked = models.ForeignKey(
        ErspOked,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="ОКЭД",
        help_text="Из справочника ЕРСП; название показывается из него.",
    )
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
        "номер свидетельства о регистрации", max_length=50, blank=True
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
    charter_capital_currency = models.ForeignKey(
        ErspCurrency,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="валюта капитала",
        help_text="Из справочника ЕРСП. Если капитал указан, а валюта нет - сум (провайдер отдаёт капитал в сумах).",
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
                condition=Q(charter_capital__isnull=True) | Q(charter_capital_currency__isnull=False),
                name="counterparties_legalentity_capital_has_currency",
                violation_error_message="У уставного капитала должна быть валюта.",
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

    def journal_root(self):
        return self.card()

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
        self.okpo = remove_spaces(self.okpo)
        self.soato_code = remove_spaces(self.soato_code)
        self.soato_name = squash_spaces(self.soato_name)
        self.reg_certificate_number = squash_spaces(self.reg_certificate_number)
        if self.charter_capital is not None and self.charter_capital_currency_id is None:
            # Провайдер ЕРСП отдаёт капитал в сумах; сотрудник может выбрать другую валюту.
            if ErspCurrency.objects.filter(pk=ErspCurrency.UZS).exists():
                self.charter_capital_currency_id = ErspCurrency.UZS

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


# --- Физическое лицо -------------------------------------------------------------

PINFL_REGEX = r"^[0-9]{14}$"


class Individual(TrackedModel):
    """
    Данные физического лица (одна строка на карточку физлица).

    Общая карточка физлица (решение Бекзода 22.10): набор полей - как у `person`
    в ЕРСП и у агента-физлица, и у участников договора (страхователь,
    выгодоприобретатель, застрахованный, владелец объекта): ФИО, пол, дата
    рождения, ПИНФЛ, паспорт (серия, номер, кем и когда выдан), водительское
    удостоверение, тип резидентства и страна. Телефон, эл. почта, регион и адрес
    лежат в самой карточке контрагента. Остальное из этапа «Физлица» (чёрный
    список, объекты) добавится без переноса этих полей.

    У иностранца (тип резидентства 2) ПИНФЛ нет, а если он указан, иностранцем
    быть нельзя (правило ЕРСП); у гражданина ПИНФЛ нужен - это проверяет
    готовность к ЕРСП, а не база. Отчество «ХХХ» (его отсутствие) в базе пусто:
    «ХХХ» подставляется при отправке в ЕРСП.

    Данные хранятся обычным текстом (решение Бекзода 22.10), как ИНН.
    Заполненный ПИНФЛ уникален в пределах страховой компании: в ЕРСП агент
    с таким ПИНФЛ у одной страховой может быть только один. Страховая компания хранится
    и здесь (копия из карточки, заполняется автоматически), чтобы это правило
    и совпадение с карточкой проверяла сама база (составная ссылка, миграция 0007);
    карточка при этом обязана быть физлицом (триггер в той же миграции).
    """

    counterparty = models.OneToOneField(
        Counterparty,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="individual",
        verbose_name="контрагент",
    )
    insurance_company = models.ForeignKey(
        InsuranceCompany,
        on_delete=models.PROTECT,
        editable=False,
        related_name="+",
        verbose_name="страховая компания",
    )
    last_name = models.CharField("фамилия", max_length=255)
    first_name = models.CharField("имя", max_length=255)
    middle_name = models.CharField("отчество", max_length=255, blank=True)
    class Gender(models.TextChoices):
        MALE = "m", "мужской"
        FEMALE = "f", "женский"

    pinfl = models.CharField(
        "ПИНФЛ",
        max_length=14,
        blank=True,
        validators=[RegexValidator(PINFL_REGEX, "ПИНФЛ - ровно 14 цифр.")],
        help_text="У иностранца ПИНФЛ нет - поле пустое. У гражданина обязателен для ЕРСП.",
    )
    gender = models.CharField("пол", max_length=1, choices=Gender.choices, blank=True)
    passport_series = models.CharField("серия паспорта", max_length=10, blank=True)
    passport_number = models.CharField("номер паспорта", max_length=20, blank=True)
    passport_issued_by = models.CharField("кем выдан паспорт", max_length=255, blank=True)
    passport_issue_date = models.DateField("когда выдан паспорт", null=True, blank=True)
    driver_license_series = models.CharField("серия водительского удостоверения", max_length=10, blank=True)
    driver_license_number = models.CharField("номер водительского удостоверения", max_length=20, blank=True)
    birth_date = models.DateField("дата рождения")
    resident_type = models.ForeignKey(
        ErspResidentType,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="тип резидентства",
        help_text="Из справочника ЕРСП. Для агента-физлица обязателен.",
    )
    country = models.ForeignKey(
        ErspCountry,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="страна",
        help_text="Из справочника ЕРСП (Узбекистан - 210). Для агента-физлица обязательна.",
    )

    class Meta:
        verbose_name = "данные физлица"
        verbose_name_plural = "данные физлиц"
        constraints = [
            models.UniqueConstraint(
                fields=["insurance_company", "pinfl"],
                condition=~Q(pinfl=""),
                name="counterparties_individual_pinfl_unique",
                violation_error_message="Физлицо с таким ПИНФЛ уже есть.",
            ),
            models.CheckConstraint(
                condition=Q(pinfl="") | Q(pinfl__regex=PINFL_REGEX),
                name="counterparties_individual_pinfl",
                violation_error_message="ПИНФЛ - ровно 14 цифр.",
            ),
            models.CheckConstraint(
                condition=~(Q(resident_type=ErspResidentType.FOREIGNER) & ~Q(pinfl="")),
                name="counterparties_individual_foreigner_no_pinfl",
                violation_error_message="У иностранца не может быть ПИНФЛ (правило ЕРСП).",
            ),
            models.CheckConstraint(
                condition=Q(gender__in=["", "m", "f"]),
                name="counterparties_individual_gender",
                violation_error_message="Пол - мужской или женский.",
            ),
            models.CheckConstraint(
                condition=Q(driver_license_series="") | Q(driver_license_series__regex=r"^[A-Z0-9]{1,10}$"),
                name="counterparties_individual_license_series",
                violation_error_message="Серия водительского удостоверения - латинские буквы и цифры.",
            ),
            models.CheckConstraint(
                condition=~Q(last_name="") & ~Q(first_name=""),
                name="counterparties_individual_names",
                violation_error_message="Фамилия и имя обязательны.",
            ),
            models.CheckConstraint(
                condition=Q(passport_series="") | Q(passport_series__regex=r"^[A-Z0-9]{1,10}$"),
                name="counterparties_individual_passport_series",
                violation_error_message="Серия паспорта - латинские буквы и цифры, без пробелов.",
            ),
            models.CheckConstraint(
                condition=Q(passport_number="") | Q(passport_number__regex=r"^[A-Z0-9]{1,20}$"),
                name="counterparties_individual_passport_number",
                violation_error_message="Номер паспорта - латинские буквы и цифры, без пробелов.",
            ),
        ]

    def __str__(self):
        return f"{self.last_name} {self.first_name}".strip()

    def card(self):
        """Карточка физлица (в форме новая карточка ещё не сохранена - см. LegalEntity.card)."""
        counterparty = getattr(self, "counterparty", None)
        if counterparty is not None:
            return counterparty
        return getattr(self, "card_in_form", None)

    def journal_root(self):
        return self.card()

    @property
    def passport(self):
        """Серия и номер слитно: «AD0000000» - так их принимает провайдер."""
        return f"{self.passport_series}{self.passport_number}"

    def normalize(self):
        counterparty = self.card()
        if counterparty is not None:
            self.insurance_company_id = counterparty.insurance_company_id
        self.last_name = squash_spaces(self.last_name)
        self.first_name = squash_spaces(self.first_name)
        self.middle_name = squash_spaces(self.middle_name)
        self.pinfl = remove_spaces(self.pinfl or "")
        self.passport_series = remove_spaces(self.passport_series or "").upper()
        self.passport_number = remove_spaces(self.passport_number or "").upper()
        self.passport_issued_by = squash_spaces(self.passport_issued_by)
        self.driver_license_series = remove_spaces(self.driver_license_series or "").upper()
        self.driver_license_number = remove_spaces(self.driver_license_number or "").upper()

    def clean(self):
        errors = []
        if self.resident_type_id == ErspResidentType.FOREIGNER and remove_spaces(self.pinfl or ""):
            raise ValidationError({"pinfl": "У иностранца не может быть ПИНФЛ (правило ЕРСП)."})
        counterparty = self.card()
        if counterparty is not None and counterparty.kind != Counterparty.Kind.INDIVIDUAL:
            errors.append("Данные физлица - только у карточки физического лица.")
        # Повтор ПИНФЛ: то же правило есть в базе, но со страховой, которой в форме
        # нет, - такие правила Django в форме не проверяет (ловушка из AGENTS.md).
        company_id = counterparty.insurance_company_id if counterparty is not None else None
        pinfl = remove_spaces(self.pinfl or "")
        if company_id and pinfl:
            same = (
                Individual.objects.filter(insurance_company_id=company_id, pinfl=pinfl)
                .exclude(pk=self.pk)
                .select_related("counterparty")
                .first()
            )
            if same is not None:
                raise ValidationError({"pinfl": f"Физлицо с таким ПИНФЛ уже есть: {same.counterparty}."})
        if errors:
            raise ValidationError(errors)

    def save(self, *args, by=None, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "insurance_company"}
        super().save(*args, by=by, **kwargs)
        self.counterparty.refresh_from_individual(self, by=by)


# --- Представители и счета (вложенные записи карточки) -------------------------------

LOCAL_ACCOUNT_REGEX = r"^[0-9]{20}$"
FOREIGN_ACCOUNT_REGEX = r"^[A-Z0-9]{5,34}$"  # IBAN - до 34 знаков


class CardChild(TrackedModel):
    """Общее для вложенных записей карточки: страховая и «История» - от карточки."""

    class Meta:
        abstract = True

    @property
    def insurance_company_id(self):
        # Своей колонки нет: страховая берётся из карточки (для журнала).
        counterparty = getattr(self, "counterparty", None)
        return counterparty.insurance_company_id if counterparty is not None else None

    def journal_root(self):
        return getattr(self, "counterparty", None)


class Representative(CardChild):
    """
    Представитель организации: кто подписывает от её имени.

    Решение Бекзода 20.10: одновременно может действовать несколько
    представителей (директор и заместитель по доверенности); один из них -
    основной, его данные уходят в ЕРСП. Сменился руководитель - старой записи
    ставится дата «действует по», заводится новая. Удалять можно только строку,
    заведённую по ошибке (решение Бекзода 25.10): через галочку «Удалить»
    в карточке, а в «Истории» остаётся след - кто, когда и что удалил.

    Формат серии и номера паспорта пока не проверяется: решим после разбора
    физлиц (представитель может быть иностранцем). ПИНФЛ - 14 цифр;
    контрольная цифра - после сверки с официальным источником.
    """

    # Строка, заведённая по ошибке, удаляется (audit.models.TrackedModel.delete_tracked);
    # если на представителя сошлётся другая таблица с защитой PROTECT, удаление откажет.
    allow_tracked_delete = True

    counterparty = models.ForeignKey(
        Counterparty,
        on_delete=models.PROTECT,
        related_name="representatives",
        verbose_name="контрагент",
    )
    full_name = models.CharField("ФИО", max_length=255)
    position = models.ForeignKey(
        Position, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+", verbose_name="должность",
    )
    position_other = models.CharField(
        "должность (если «Иное»)", max_length=255, blank=True
    )
    power_basis = models.ForeignKey(
        PowerBasis, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+", verbose_name="основание полномочий",
    )
    power_basis_other = models.CharField(
        "основание (если «Иное»)", max_length=255, blank=True
    )
    power_basis_number = models.CharField("номер документа", max_length=100, blank=True)
    pinfl = models.CharField(
        "ПИНФЛ",
        max_length=14,
        blank=True,
        validators=[RegexValidator(PINFL_REGEX, "ПИНФЛ - ровно 14 цифр.")],
    )
    passport_series = models.CharField("серия паспорта", max_length=10, blank=True)
    passport_number = models.CharField("номер паспорта", max_length=20, blank=True)
    valid_from = models.DateField("действует с", null=True, blank=True)
    # Пояснения к полям убраны из формы 20.10 - сдвигали поля в строке:
    # «действует по» пусто - действует без срока; данные основного уходят в ЕРСП.
    valid_to = models.DateField("действует по", null=True, blank=True)
    is_primary = models.BooleanField("основной", default=False)

    class Meta:
        verbose_name = "представитель"
        verbose_name_plural = "представители"
        ordering = ["-is_primary", "valid_to", "full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["counterparty"],
                condition=Q(is_primary=True),
                name="counterparties_representative_one_primary",
                violation_error_message="Основным может быть только один представитель.",
            ),
            models.CheckConstraint(
                condition=Q(pinfl="") | Q(pinfl__regex=PINFL_REGEX),
                name="counterparties_representative_pinfl",
                violation_error_message="ПИНФЛ - ровно 14 цифр.",
            ),
            models.CheckConstraint(
                condition=Q(valid_from__isnull=True)
                | Q(valid_to__isnull=True)
                | Q(valid_to__gte=models.F("valid_from")),
                name="counterparties_representative_dates",
                violation_error_message="«Действует по» не может быть раньше «действует с».",
            ),
        ]

    def __str__(self):
        return self.full_name

    def normalize(self):
        self.full_name = squash_spaces(self.full_name)
        self.position_other = squash_spaces(self.position_other)
        self.power_basis_other = squash_spaces(self.power_basis_other)
        self.power_basis_number = squash_spaces(self.power_basis_number)
        self.pinfl = remove_spaces(self.pinfl)
        self.passport_series = remove_spaces(self.passport_series).upper()
        self.passport_number = remove_spaces(self.passport_number).upper()
        # Текст «Иное» хранится, только если выбран пункт «Иное».
        if self.position_id is None or not self.position.is_other:
            self.position_other = ""
        if self.power_basis_id is None or not self.power_basis.is_other:
            self.power_basis_other = ""

    def clean(self):
        errors = {}
        counterparty = getattr(self, "counterparty", None)
        if counterparty is not None and counterparty.kind != Counterparty.Kind.LEGAL_ENTITY:
            errors["full_name"] = "Представители заводятся только у организаций."
        if self.position_id and self.position.is_other and not self.position_other:
            errors["position_other"] = "Выбрано «Иное» - напишите должность."
        if self.power_basis_id and self.power_basis.is_other and not self.power_basis_other:
            errors["power_basis_other"] = "Выбрано «Иное» - напишите основание."
        if self.is_primary and self.valid_to and self.valid_to < timezone.localdate():
            errors["is_primary"] = "Срок полномочий истёк - основным он быть не может."
        if errors:
            raise ValidationError(errors)

    @property
    def position_text(self):
        """Должность текстом - как она уйдёт в ЕРСП (position)."""
        if self.position_id is None:
            return ""
        return self.position_other if self.position.is_other else self.position.name

    @property
    def power_basis_text(self):
        """Основание текстом - как оно уйдёт в ЕРСП (representativeDoc)."""
        if self.power_basis_id is None:
            return ""
        return self.power_basis_other if self.power_basis.is_other else self.power_basis.name


class BankAccount(CardChild):
    """
    Банковский счёт контрагента.

    Банк - либо из справочника (банк Узбекистана, по МФО), либо, для
    иностранного банка, название текстом. Местный счёт - ровно 20 цифр;
    иностранный - от 5 до 34 латинских букв и цифр (IBAN и другие).
    В ЕРСП уходит основной счёт; основной - только один и не закрытый.
    Закрытый счёт не удаляется, а получает отметку «закрыт».

    Проверка, что код валюты в номере счёта (6-8 знаки) совпадает с валютой
    и контрольный ключ с МФО - после сверки с официальным источником.
    """

    counterparty = models.ForeignKey(
        Counterparty, on_delete=models.PROTECT, related_name="accounts", verbose_name="контрагент"
    )
    bank = models.ForeignKey(
        Bank, on_delete=models.PROTECT, null=True, blank=True,
        related_name="accounts", verbose_name="банк (Узбекистан)",
    )
    foreign_bank_name = models.CharField(
        "иностранный банк", max_length=255, blank=True,
        help_text="Только если банка нет в справочнике (иностранный банк).",
    )
    number = models.CharField("номер счёта", max_length=34)
    currency = models.ForeignKey(
        ErspCurrency, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+", verbose_name="валюта",
        help_text="Из справочника ЕРСП. Пусто - сум.",
    )
    is_primary = models.BooleanField(
        "основной", default=False, help_text="Уходит в ЕРСП. Основной - только один."
    )
    is_closed = models.BooleanField("закрыт", default=False)

    class Meta:
        verbose_name = "банковский счёт"
        verbose_name_plural = "банковские счета"
        ordering = ["-is_primary", "is_closed", "number"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(bank__isnull=False) & Q(foreign_bank_name=""))
                | (Q(bank__isnull=True) & ~Q(foreign_bank_name="")),
                name="counterparties_bankaccount_one_bank",
                violation_error_message="Укажите банк из справочника или иностранный банк - что-то одно.",
            ),
            models.CheckConstraint(
                condition=Q(bank__isnull=True) | Q(number__regex=LOCAL_ACCOUNT_REGEX),
                name="counterparties_bankaccount_local_number",
                violation_error_message="Счёт в банке Узбекистана - ровно 20 цифр.",
            ),
            models.CheckConstraint(
                condition=Q(bank__isnull=False) | Q(number__regex=FOREIGN_ACCOUNT_REGEX),
                name="counterparties_bankaccount_foreign_number",
                violation_error_message="Счёт в иностранном банке - от 5 до 34 латинских букв и цифр.",
            ),
            models.CheckConstraint(
                condition=Q(is_primary=False) | Q(is_closed=False),
                name="counterparties_bankaccount_primary_open",
                violation_error_message="Закрытый счёт не может быть основным.",
            ),
            models.CheckConstraint(
                condition=Q(currency__isnull=False),
                name="counterparties_bankaccount_has_currency",
                violation_error_message="Укажите валюту счёта.",
            ),
            models.UniqueConstraint(
                fields=["counterparty"],
                condition=Q(is_primary=True),
                name="counterparties_bankaccount_one_primary",
                violation_error_message="Основным может быть только один счёт.",
            ),
            models.UniqueConstraint(
                fields=["counterparty", "number"],
                name="counterparties_bankaccount_number_unique",
                violation_error_message="Такой счёт у контрагента уже есть.",
            ),
        ]

    def __str__(self):
        bank = self.bank.mfo if self.bank_id else self.foreign_bank_name
        return f"{self.number} ({bank})"

    def clean(self):
        # Правило есть в базе, но в форме админ-панели отметка «основной»
        # проверяется по всему блоку, а не построчно (см. CardChildForm) -
        # поэтому дублируем его здесь.
        if self.is_primary and self.is_closed:
            raise ValidationError({"is_primary": "Закрытый счёт не может быть основным."})

    def normalize(self):
        self.number = remove_spaces(self.number or "").upper()
        self.foreign_bank_name = squash_spaces(self.foreign_bank_name)
        if self.bank_id is not None:
            self.foreign_bank_name = ""
        if self.currency_id is None and ErspCurrency.objects.filter(pk=ErspCurrency.UZS).exists():
            self.currency_id = ErspCurrency.UZS
