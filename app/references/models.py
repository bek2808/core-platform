"""
Справочники ЕРСП (единый реестр страховых полисов): регионы, формы собственности,
страховщики.

Откуда данные. Это копии справочников ЕРСП: их не правят руками, а загружают
командой load_ersp_references (сейчас из файлов в references/data/ersp/, позже -
прямо из ЕРСП). Главный ключ каждой записи - её id в ЕРСП: именно его мы
отправляем в ЕРСП (regionId, ownershipFormId), поэтому он должен совпадать.

Записи никогда не удаляются. Если строка пропала из ответа ЕРСП, она получает
отметку «нет в последней загрузке», но остаётся: на неё могут ссылаться
контрагенты и старые полисы.
"""
from django.db import models


class ErspReference(models.Model):
    """Общая часть всех справочников ЕРСП."""

    id = models.PositiveIntegerField("id в ЕРСП", primary_key=True)
    raw = models.JSONField(
        "ответ ЕРСП",
        help_text="Строка ответа ЕРСП целиком, как пришла.",
    )
    is_present = models.BooleanField(
        "есть в ЕРСП",
        default=True,
        help_text="Была ли запись в последней загрузке справочника.",
    )
    synced_at = models.DateTimeField(
        "обновлено из ЕРСП",
        help_text="Когда запись последний раз создана или изменена загрузкой.",
    )

    class Meta:
        abstract = True


class ErspRegion(ErspReference):
    """Регион (область) по справочнику ЕРСП /api/references/regions."""

    name = models.CharField("название", max_length=255)
    name_uz = models.CharField("название (узб., латиница)", max_length=255, blank=True)
    name_uzc = models.CharField("название (узб., кириллица)", max_length=255, blank=True)
    soato_code = models.CharField(
        "код области по СОАТО",
        max_length=4,
        unique=True,
        null=True,
        blank=True,
        help_text=(
            "Наше дополнение, в ЕРСП его нет (файл references/data/soato_regions.json). "
            "По первым 4 цифрам кода СОАТО района от провайдера система "
            "подсказывает регион."
        ),
    )

    class Meta:
        verbose_name = "регион ЕРСП"
        verbose_name_plural = "регионы ЕРСП"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ErspOwnershipForm(ErspReference):
    """Форма собственности по справочнику ЕРСП /api/references/ownership-forms."""

    name = models.CharField("название", max_length=255)

    class Meta:
        verbose_name = "форма собственности ЕРСП"
        verbose_name_plural = "формы собственности ЕРСП"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ErspInsurer(ErspReference):
    """
    Страховая организация по справочнику страховщиков ЕРСП.

    Серии полисов, рейтинг и разрешения по видам страхования (ОСГОП, ОСАГО и др.)
    лежат в поле raw: они меняются в ЕРСП, а правил по ним у нас пока нет.
    Когда правило появится, нужное поле станет отдельной колонкой.
    """

    class Industry(models.IntegerChoices):
        # Значения industryId в ЕРСП. Расшифровка - наш вывод: у всех
        # страховщиков с industryId = 1 разрешено страхование жизни.
        LIFE = 1, "страхование жизни"
        GENERAL = 2, "общее страхование"

    name = models.CharField("название", max_length=500)
    tin = models.CharField(
        "ИНН",
        max_length=9,
        unique=True,
        null=True,
        blank=True,
        help_text="В ЕРСП бывает пустым; заполненный ИНН уникален.",
    )
    industry = models.PositiveSmallIntegerField(
        "вид деятельности", choices=Industry.choices, null=True, blank=True
    )
    license_stopped_date = models.DateField(
        "лицензия остановлена", null=True, blank=True
    )

    class Meta:
        verbose_name = "страховщик ЕРСП"
        verbose_name_plural = "страховщики ЕРСП"
        ordering = ["name"]

    def __str__(self):
        return self.name
