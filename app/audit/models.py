"""
Аудит: кто и когда создал и изменил запись.

TrackedModel - общая основа для всех таблиц, которые правят люди
(контрагенты, банки, справочники для ввода, позже полисы и убытки).
В каждой такой записи есть четыре поля: когда создана и кем, когда изменена
и кем. Время - в UTC.

Как передать «кем». Сохранение вызывается с указанием сотрудника:
    bank.save(by=request.user)
Админ-панель делает это сама (audit.admin.TrackedAdmin). Если сотрудник
не указан (команда загрузки, миграция), поле остаётся пустым: это значит
«система», а не «неизвестно кто».

Нормализация. У записи может быть метод normalize(): он приводит значения
к единому виду (обрезает пробелы, убирает лишние символы). Он вызывается
перед проверкой формата и перед каждым сохранением, поэтому одинаково
работает и в админ-панели, и при загрузке данных от провайдера.

В следующем шаге здесь же появится журнал изменений (было -> стало).
"""
from django.conf import settings
from django.db import models


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

    class Meta:
        abstract = True

    def normalize(self):
        """Привести значения к единому виду. Своя у каждой таблицы."""

    def clean_fields(self, exclude=None):
        # Сначала нормализуем, потом проверяем формат: « 01095 » - это МФО 01095.
        self.normalize()
        super().clean_fields(exclude=exclude)

    def save(self, *args, by=None, **kwargs):
        """
        Сохранить запись. by - сотрудник, который вносит изменение
        (None - система).
        """
        self.normalize()
        if self._state.adding:
            self.created_by = by
        self.updated_by = by
        # Если сохраняются только отдельные поля, отметка «кто изменил»
        # всё равно должна попасть в базу.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "updated_by", "updated_at"}
        super().save(*args, **kwargs)
