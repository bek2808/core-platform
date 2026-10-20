"""
Аудит: кто и когда создал и изменил запись, и журнал изменений (было -> стало).

TrackedModel - общая основа для всех таблиц, которые правят люди
(контрагенты, банки, справочники для ввода, позже полисы и убытки).

1. Кто и когда. В каждой записи четыре поля: когда создана и кем, когда
   изменена и кем. Время - в UTC.

2. Журнал изменений (ChangeLog). Каждое сохранение сравнивает запись с тем,
   что лежит в базе, и пишет в журнал каждое изменённое поле: было -> стало,
   кто, когда, источник. Все поля одного сохранения связаны общим номером
   (change_id). Запись в таблицу и запись в журнал идут в одной транзакции:
   либо сохранилось и то, и другое, либо ничего.

Как сохранять:
    bank.save(by=request.user)                 # сотрудник, вручную
    bank.save(by=user, source=Source.PROVIDER) # данные от провайдера ЕРСП
    bank.save()                                # система (загрузка, миграция)
Админ-панель передаёт сотрудника сама (audit.admin.TrackedAdmin).

Обойти журнал нельзя. Массовые операции (QuerySet.update(), delete(),
bulk_create(), bulk_update()) и удаление записи для этих таблиц запрещены:
они меняют базу без сохранения записи и, значит, без журнала.

Сам журнал только пополняется: правку и удаление его строк запрещает
триггер в PostgreSQL (миграция audit/0001_initial), для всех ролей,
включая роль приложения. Ограничение: владелец таблицы может снять
триггер миграцией - но такая миграция пройдёт через git и через человека.

Нормализация. У записи может быть метод normalize(): он приводит значения
к единому виду (обрезает пробелы, убирает лишние символы). Он вызывается
перед проверкой формата и перед каждым сохранением, поэтому « 01095 » и
«01095» - одно и то же значение и не считается изменением.
"""
import uuid

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models, router, transaction
from django.utils import timezone

# Служебные поля: в журнал не пишутся, у них своя роль.
SERVICE_FIELDS = {"created_at", "created_by", "updated_at", "updated_by"}


class Source(models.TextChoices):
    MANUAL = "manual", "вручную"
    PROVIDER = "provider", "провайдер ЕРСП"
    SYSTEM = "system", "система"


class UntrackedChangeError(Exception):
    """Попытка изменить таблицу с журналом в обход сохранения записи."""


class TrackedQuerySet(models.QuerySet):
    """Запрещает массовые операции, которые обходят журнал изменений."""

    def _forbid(self, operation):
        raise UntrackedChangeError(
            f"{self.model._meta.verbose_name_plural}: {operation} запрещено - изменение "
            "не попадёт в журнал. Меняйте записи по одной через save(by=...); "
            "удаление заменяется архивом или отметкой «действует»."
        )

    def update(self, **kwargs):
        self._forbid("массовое изменение (update)")

    def delete(self):
        self._forbid("удаление")

    def bulk_create(self, *args, **kwargs):
        self._forbid("массовое создание (bulk_create)")

    def bulk_update(self, *args, **kwargs):
        self._forbid("массовое изменение (bulk_update)")


class TrackedModel(models.Model):
    created_at = models.DateTimeField("создана", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="создал",
        help_text="Пусто - запись создана системой (загрузка, миграция).",
    )
    updated_at = models.DateTimeField("изменена", auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="изменил",
        help_text="Пусто - последнее изменение сделала система.",
    )

    # Поля, которые не пишутся в журнал (например, служебная копия
    # для поиска). Задаётся в конкретной таблице.
    audit_exclude = ()

    objects = TrackedQuerySet.as_manager()

    class Meta:
        abstract = True

    def normalize(self):
        """Привести значения к единому виду. Своя у каждой таблицы."""

    def journal_root(self):
        """
        Карточка, в «Истории» которой видны изменения этой записи.

        По умолчанию - сама запись. Вложенные записи (представитель, счёт)
        возвращают свою карточку контрагента: тогда «История» карточки
        показывает и их изменения.
        """
        return None

    def clean_fields(self, exclude=None):
        # Сначала нормализуем, потом проверяем формат: « 01095 » - это МФО 01095.
        self.normalize()
        super().clean_fields(exclude=exclude)

    def save(self, *args, by=None, source=None, **kwargs):
        """
        Сохранить запись и записать изменения в журнал.
        by - сотрудник (None - система); source - откуда изменение
        (по умолчанию «вручную», если указан сотрудник, иначе «система»).
        """
        if source is None:
            source = Source.MANUAL if by is not None else Source.SYSTEM
        self.normalize()
        adding = self._state.adding
        if adding:
            self.created_by = by
        self.updated_by = by

        update_fields = kwargs.get("update_fields")
        fields = self._journal_fields()
        if update_fields is not None:
            fields = [f for f in fields if f.name in update_fields or f.attname in update_fields]
            # Отметка «кто изменил» должна попасть в базу и при частичном сохранении.
            kwargs["update_fields"] = {*update_fields, "updated_by", "updated_at"}

        with transaction.atomic(using=router.db_for_write(type(self), instance=self)):
            before = None if adding else self._values_in_database(fields)
            super().save(*args, **kwargs)
            write_journal(self, fields, before, by=by, source=source)

    def delete(self, *args, **kwargs):
        raise UntrackedChangeError(
            f"{self._meta.verbose_name}: удаление запрещено. "
            "Запись выводится из работы (архив, отметка «действует»)."
        )

    def _journal_fields(self):
        return [
            f
            for f in self._meta.concrete_fields
            if not f.primary_key
            and f.name not in SERVICE_FIELDS
            and f.name not in self.audit_exclude
        ]

    def _values_in_database(self, fields):
        """
        Значения полей, как они лежат в базе сейчас (None - записи ещё нет).

        Строка блокируется до конца транзакции (select_for_update): если два
        сотрудника сохраняют одну запись одновременно, второй дождётся
        первого и запишет в журнал точное «было» - уже с правкой первого.
        """
        return (
            type(self)._base_manager.select_for_update()
            .filter(pk=self.pk)
            .values(*[f.attname for f in fields])
            .first()
        )


def write_journal(instance, fields, before, *, by, source):
    """
    Записать в журнал отличия instance от before (значения из базы).
    before = None - запись новая: пишутся все заполненные поля.
    """
    created = before is None
    content_type = ContentType.objects.get_for_model(type(instance))
    root = instance.journal_root()
    root_type = ContentType.objects.get_for_model(type(root)) if root is not None else None
    change_id = uuid.uuid4()
    entries = []
    for field in fields:
        new = field.to_python(field.value_from_object(instance))
        old = None if created else field.to_python(before[field.attname])
        if created and new in (None, ""):
            continue  # у новой записи пустые поля не интересны
        if not created and old == new:
            continue
        entries.append(
            ChangeLog(
                change_id=change_id,
                user=by,
                source=source,
                action=ChangeLog.Action.CREATE if created else ChangeLog.Action.UPDATE,
                content_type=content_type,
                object_id=instance.pk,
                object_repr=str(instance)[:255],
                root_content_type=root_type,
                root_object_id=root.pk if root is not None else None,
                insurance_company_id=getattr(instance, "insurance_company_id", None),
                field=field.name,
                old_value=old,
                new_value=new,
            )
        )
    ChangeLog.objects.bulk_create(entries)


class ChangeLog(models.Model):
    """
    Журнал изменений: одна строка - одно изменённое поле одной записи.
    Только пополняется: правку и удаление запрещает триггер в базе.
    """

    class Action(models.TextChoices):
        CREATE = "create", "создание"
        UPDATE = "update", "изменение"

    changed_at = models.DateTimeField("когда", default=timezone.now, db_index=True)
    change_id = models.UUIDField(
        "номер изменения",
        help_text="Общий у всех полей, изменённых одним сохранением.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="кто",
        help_text="Пусто - система (загрузка, миграция).",
    )
    source = models.CharField("источник", max_length=16, choices=Source.choices)
    action = models.CharField("действие", max_length=16, choices=Action.choices)
    content_type = models.ForeignKey(
        ContentType, on_delete=models.PROTECT, related_name="+", verbose_name="таблица"
    )
    object_id = models.PositiveBigIntegerField("номер записи")
    object_repr = models.CharField(
        "запись", max_length=255, help_text="Как запись называлась в момент изменения."
    )
    insurance_company = models.ForeignKey(
        "companies.InsuranceCompany",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="страховая компания",
        help_text="Пусто - общий справочник (банки, должности).",
    )
    root_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="таблица карточки",
        help_text="Для вложенных записей (представитель, счёт) - чья это карточка.",
    )
    root_object_id = models.PositiveBigIntegerField("номер карточки", null=True, blank=True)
    field = models.CharField("поле", max_length=64)
    old_value = models.JSONField("было", null=True, blank=True, encoder=DjangoJSONEncoder)
    new_value = models.JSONField("стало", null=True, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        verbose_name = "изменение"
        verbose_name_plural = "журнал изменений"
        ordering = ["-changed_at", "-id"]
        indexes = [
            models.Index(fields=["content_type", "object_id"], name="audit_changelog_object"),
            models.Index(
                fields=["root_content_type", "root_object_id"], name="audit_changelog_root"
            ),
        ]

    def __str__(self):
        return f"{self.object_repr}: {self.field}"
