"""
Хранилище файлов: загруженный файл и журнал просмотров (docs/files-model.md).

Обе таблицы только пополняются: запись нельзя изменить и удалить - ни
из приложения (запрет здесь), ни из консоли (триггер в базе, files/0001).
Замена скана - новый файл; прежний остаётся и открывается из истории.
Так то, что уже ушло в ЕРСП, нельзя подменить задним числом.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class AppendOnlyError(Exception):
    """Попытка изменить или удалить запись, которая только пополняется."""


class AppendOnlyQuerySet(models.QuerySet):
    def _forbid(self, operation):
        raise AppendOnlyError(
            f"{self.model._meta.verbose_name_plural}: {operation} запрещено - "
            "записи только пополняются. Замена файла - новый файл."
        )

    def update(self, **kwargs):
        self._forbid("массовое изменение (update)")

    def bulk_update(self, *args, **kwargs):
        self._forbid("массовое изменение (bulk_update)")

    def delete(self):
        self._forbid("удаление")


class AppendOnlyModel(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError(f"{self._meta.verbose_name}: запись не меняется после создания.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{self._meta.verbose_name}: удаление запрещено.")


class StoredFile(AppendOnlyModel):
    """
    Загруженный файл. Сам файл лежит на диске (storage.py), здесь - кто, к чему,
    какой и его отпечаток. Создаётся только функцией storage.store().
    """

    id = models.UUIDField("номер", primary_key=True, default=uuid.uuid4, editable=False)
    insurance_company = models.ForeignKey(
        "companies.InsuranceCompany", on_delete=models.PROTECT, related_name="+",
        verbose_name="страховая компания",
    )
    purpose = models.CharField(
        "назначение", max_length=32,
        help_text="Что это за файл, например «agreement_scan» - скан соглашения (registry.py).",
    )
    owner_id = models.PositiveBigIntegerField(
        "номер записи", help_text="Запись, к которой приложен файл; её вид определяет назначение.",
    )
    original_name = models.CharField("исходное имя", max_length=255)
    content_type = models.CharField("тип", max_length=100)
    size = models.PositiveBigIntegerField("размер, байт")
    sha256 = models.CharField("отпечаток SHA-256", max_length=64)
    path = models.CharField(
        "путь", max_length=255, unique=True,
        help_text="Относительно папки хранилища; имя на диске - случайное.",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+", verbose_name="загрузил", help_text="Пусто - система.",
    )
    uploaded_at = models.DateTimeField("загружен", default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "файл"
        verbose_name_plural = "файлы"
        ordering = ["-uploaded_at", "-id"]
        indexes = [models.Index(fields=["purpose", "owner_id"], name="files_storedfile_owner")]
        constraints = [
            models.CheckConstraint(
                condition=Q(size__gt=0), name="files_storedfile_not_empty",
                violation_error_message="Файл не может быть пустым.",
            ),
            models.CheckConstraint(
                condition=Q(content_type__in=["application/pdf", "image/jpeg", "image/png"]),
                name="files_storedfile_type",
            ),
            models.CheckConstraint(
                condition=Q(sha256__regex=r"^[0-9a-f]{64}$"), name="files_storedfile_sha256",
            ),
        ]

    def __str__(self):
        return self.original_name


class FileView(AppendOnlyModel):
    """Журнал просмотров: кто и когда открыл файл (в сканах бывают паспортные данные)."""

    file = models.ForeignKey(StoredFile, on_delete=models.PROTECT, related_name="views", verbose_name="файл")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+", verbose_name="кто",
    )
    viewed_at = models.DateTimeField("когда", default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "просмотр файла"
        verbose_name_plural = "журнал просмотров файлов"
        ordering = ["-viewed_at", "-id"]

    def __str__(self):
        return f"{self.file}: {self.user} {self.viewed_at:%d.%m.%Y %H:%M}"
