"""
Справочник контрагентов.

Сейчас здесь справочники для ввода: банки, основания полномочий
и должности представителей. Карточка контрагента, данные юрлица,
представители и счета добавляются следующими шагами.
Полное описание модели - docs/counterparties-model.md.

Общие правила:
- записи не удаляются: на них будут ссылаться контрагенты и полисы;
  вместо удаления снимается отметка «действует»;
- кто и когда создал и изменил запись - поля из audit.TrackedModel;
- проверки формата продублированы в базе (CheckConstraint): неверное
  значение не попадёт в таблицу, даже если его запишут в обход формы.
"""
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from audit.models import TrackedModel

from .normalize import remove_spaces, squash_spaces

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
