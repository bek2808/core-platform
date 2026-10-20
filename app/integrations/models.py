"""
Журнал обмена с внешними системами (сейчас - ЕРСП).

Одна строка - один запрос: что отправили, что ответили, в какую среду,
кто и когда, сколько ждали ответа и чем кончилось. Здесь же хранится
ответ провайдера целиком («ответ храним с датой» - решение Бекзода 20.10).

Секретов в журнале нет: пароль, client_secret и токены заменяются на ***
до записи (integrations.ersp.mask). Заголовок с токеном не пишется вовсе.

Журнал только пополняется: правку и удаление его строк запрещает
триггер в PostgreSQL (миграция integrations/0001), для всех ролей.
"""
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone


class Environment(models.TextChoices):
    PROD = "prod", "боевая"
    SANDBOX = "sandbox", "песочница"


class Exchange(models.Model):
    class Operation(models.TextChoices):
        TOKEN = "token", "получение токена"
        PROVIDER_INN = "provider_inn", "провайдер: организация по ИНН"

    class Outcome(models.TextChoices):
        OK = "ok", "успешно"
        ERSP_ERROR = "ersp_error", "ЕРСП вернул ошибку"
        HTTP_ERROR = "http_error", "ошибка HTTP"
        NETWORK_ERROR = "network_error", "нет связи"
        BAD_RESPONSE = "bad_response", "непонятный ответ"

    started_at = models.DateTimeField("когда", default=timezone.now, db_index=True)
    duration_ms = models.PositiveIntegerField("ждали ответа, мс", null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="кто",
        help_text="Пусто - система (команда на сервере, расписание).",
    )
    insurance_company = models.ForeignKey(
        "companies.InsuranceCompany",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="страховая компания",
        help_text="От чьего имени (чьими данными для входа) шёл запрос.",
    )
    environment = models.CharField("среда", max_length=16, choices=Environment.choices)
    operation = models.CharField("операция", max_length=32, choices=Operation.choices)
    subject = models.CharField(
        "что запрашивали",
        max_length=100,
        blank=True,
        db_index=True,
        help_text="Например, ИНН организации - для поиска по журналу.",
    )
    method = models.CharField("метод", max_length=8)
    url = models.CharField("адрес", max_length=500)
    request_body = models.JSONField(
        "запрос", null=True, blank=True, encoder=DjangoJSONEncoder,
        help_text="Секреты заменены на ***.",
    )
    http_status = models.PositiveSmallIntegerField("код HTTP", null=True, blank=True)
    response_body = models.JSONField(
        "ответ", null=True, blank=True, encoder=DjangoJSONEncoder,
        help_text="Ответ целиком; токены заменены на ***.",
    )
    response_text = models.TextField(
        "ответ текстом", blank=True,
        help_text="Если ответ - не JSON (например, страница ошибки): начало ответа.",
    )
    outcome = models.CharField("итог", max_length=16, choices=Outcome.choices)
    error_code = models.CharField("код ошибки ЕРСП", max_length=50, blank=True)
    error_message = models.TextField("сообщение об ошибке", blank=True)

    class Meta:
        verbose_name = "обмен с ЕРСП"
        verbose_name_plural = "журнал обмена"
        ordering = ["-started_at", "-id"]

    def __str__(self):
        subject = f" {self.subject}" if self.subject else ""
        return f"{self.get_operation_display()}{subject} ({self.get_environment_display()})"
