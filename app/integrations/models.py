"""
Обмен с внешними системами (сейчас - ЕРСП): подключение страховой к ЕРСП
и журнал обмена.

Подключение к ЕРСП (ErspConnection и ErspCredentials) - модель описана
в docs/integrations-model.md.

Журнал обмена (Exchange).

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
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone

from audit.models import TrackedModel
from config import crypto


class Environment(models.TextChoices):
    PROD = "prod", "боевая"
    SANDBOX = "sandbox", "песочница"


class Exchange(models.Model):
    class Operation(models.TextChoices):
        TOKEN = "token", "получение токена"
        PROVIDER_INN = "provider_inn", "провайдер: организация по ИНН"
        REFERENCES_AGENCIES = "references_agencies", "справочник: агенты и филиалы"

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


# --- Подключение к ЕРСП ---------------------------------------------------------------

class ErspConnection(TrackedModel):
    """
    Подключение страховой компании к ЕРСП: одна карточка на страховую.
    Внутри - данные для входа по каждой среде (ErspCredentials) и среда,
    в которую уходят полисы.

    Среды для чтения не настраиваются (решение Бекзода 20.10): справочники
    и провайдер - всегда боевая среда (в песочнице справочники неполные).
    Меняется только среда отправки полисов: по умолчанию песочница,
    переключение на боевую - с подтверждением на экране и записью в журнал.

    Карточку и оба блока сред создаёт for_company(); записи не удаляются.
    """

    insurance_company = models.OneToOneField(
        "companies.InsuranceCompany",
        on_delete=models.PROTECT,
        related_name="ersp_connection",
        verbose_name="страховая компания",
    )
    send_environment = models.CharField(
        "отправка полисов",
        max_length=16,
        choices=Environment.choices,
        default=Environment.SANDBOX,
        help_text="Куда уходят полисы. Боевая - настоящий реестр регулятора.",
    )

    class Meta:
        verbose_name = "подключение к ЕРСП"
        verbose_name_plural = "подключение к ЕРСП"
        ordering = ["insurance_company__name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(send_environment__in=Environment.values),
                name="integrations_erspconnection_send_environment_known",
                violation_error_message="Среда отправки - боевая или песочница.",
            ),
        ]

    def __str__(self):
        return f"ЕРСП: {self.insurance_company}"

    @classmethod
    def for_company(cls, company, by=None):
        """
        Карточка подключения страховой. Если её нет - создаётся вместе
        с пустыми блоками обеих сред (by - кто открыл экран; в журнал).
        """
        connection = cls.objects.filter(insurance_company=company).first()
        if connection is None:
            try:
                with transaction.atomic():
                    connection = cls(insurance_company=company)
                    connection.save(by=by)
            except IntegrityError:
                # Карточку в ту же секунду создал другой сотрудник - берём его.
                connection = cls.objects.get(insurance_company=company)
        missing = set(Environment.values) - set(connection.credentials.values_list("environment", flat=True))
        for environment in sorted(missing):
            try:
                with transaction.atomic():
                    ErspCredentials(connection=connection, environment=environment).save(by=by)
            except IntegrityError:
                pass  # блок уже создан параллельно
        return connection


class ErspCredentials(TrackedModel):
    """
    Данные для входа страховой в одну среду ЕРСП (выдаёт НАПП): client_id,
    client_secret, логин, пароль.

    client_id и логин хранятся как есть и видны на экране. client_secret
    и пароль хранятся только шифром (config.crypto) и обратно не
    показываются никому: на экране - «задан ••••». В журнал изменений
    попадает только факт смены («*** -> ***»), не значение и не шифр.

    Шифр меняется только через set_secret(): форма экрана шифров не
    видит (editable=False), поэтому проверка формата шифра в базе
    (CheckConstraint) не может дать сотруднику ошибку 500.
    """

    # Секретное значение -> поле с его шифром.
    SECRET_FIELDS = {
        "client_secret": "client_secret_cipher",
        "password": "password_cipher",
    }

    connection = models.ForeignKey(
        ErspConnection,
        on_delete=models.PROTECT,
        related_name="credentials",
        verbose_name="подключение",
    )
    environment = models.CharField("среда", max_length=16, choices=Environment.choices)
    client_id = models.CharField("client_id", max_length=255, blank=True)
    username = models.CharField("логин", max_length=255, blank=True)
    client_secret_cipher = models.TextField(
        "client_secret", blank=True, editable=False,
        help_text="Шифр (config.crypto). Значение не хранится и не показывается.",
    )
    password_cipher = models.TextField(
        "пароль", blank=True, editable=False,
        help_text="Шифр (config.crypto). Значение не хранится и не показывается.",
    )

    audit_mask = ("client_secret_cipher", "password_cipher")

    class Meta:
        verbose_name = "данные для входа в ЕРСП"
        verbose_name_plural = "данные для входа в ЕРСП"
        ordering = ["connection", "environment"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "environment"],
                name="integrations_erspcredentials_one_per_environment",
                violation_error_message="Для этой среды данные для входа уже заведены.",
            ),
            models.CheckConstraint(
                condition=Q(environment__in=Environment.values),
                name="integrations_erspcredentials_environment_known",
                violation_error_message="Среда - боевая или песочница.",
            ),
            models.CheckConstraint(
                condition=Q(client_secret_cipher="") | Q(client_secret_cipher__regex=crypto.CIPHER_REGEX),
                name="integrations_erspcredentials_client_secret_is_cipher",
                violation_error_message="client_secret хранится только шифром.",
            ),
            models.CheckConstraint(
                condition=Q(password_cipher="") | Q(password_cipher__regex=crypto.CIPHER_REGEX),
                name="integrations_erspcredentials_password_is_cipher",
                violation_error_message="Пароль хранится только шифром.",
            ),
        ]

    def __str__(self):
        return f"{self.connection.insurance_company}: {self.get_environment_display()}"

    @property
    def insurance_company_id(self):
        # Своей колонки нет: страховая берётся из карточки (для журнала).
        connection = getattr(self, "connection", None)
        return connection.insurance_company_id if connection is not None else None

    def journal_root(self):
        # Изменения видны в «Истории» карточки подключения.
        return getattr(self, "connection", None)

    def normalize(self):
        self.client_id = (self.client_id or "").strip()
        self.username = (self.username or "").strip()

    # --- Секретные значения ---------------------------------------------------------

    def _context(self, name):
        # Шифр привязан к страховой, среде и полю: в чужой строке он не расшифруется.
        return f"integrations.ErspCredentials:{self.insurance_company_id}:{self.environment}:{name}"

    def set_secret(self, name, value):
        """Задать секретное значение (зашифровать); пустая строка - стереть."""
        field = self.SECRET_FIELDS[name]
        setattr(self, field, crypto.encrypt(value, self._context(name)) if value else "")

    def has_secret(self, name):
        return bool(getattr(self, self.SECRET_FIELDS[name]))

    def secret(self, name):
        """Расшифрованное значение ("" - не задано). Ошибка - crypto.CryptoError."""
        cipher = getattr(self, self.SECRET_FIELDS[name])
        return crypto.decrypt(cipher, self._context(name)) if cipher else ""

    def missing(self):
        """Чего не хватает для входа: названия незаполненных значений."""
        names = []
        if not self.client_id:
            names.append("client_id")
        if not self.has_secret("client_secret"):
            names.append("client_secret")
        if not self.username:
            names.append("логин")
        if not self.has_secret("password"):
            names.append("пароль")
        return names

    def login_data(self):
        """
        Все четыре значения для входа (секреты расшифрованы) - только для
        запроса токена. Никуда не сохранять и не выводить.
        """
        return {
            "client_id": self.client_id,
            "client_secret": self.secret("client_secret"),
            "username": self.username,
            "password": self.secret("password"),
        }


# --- Справочник «Агенты/филиалы» ЕРСП --------------------------------------------

class ErspAgency(models.Model):
    """
    Запись справочника ЕРСП «Агенты/филиалы» (/api/references/agencies):
    головной офис, филиал или агент страховой с его agencyId. agencyId
    уходит в ЕРСП в каждом договоре, полисе, подтверждении оплаты.
    Модель: docs/users-model.md, «Связь с ЕРСП: agencyId».

    Список у каждой страховой свой (ЕРСП отдаёт только агентов своей СК),
    и в песочнице id другие, чем в боевой среде, - поэтому ключ записи -
    тройка «страховая + среда + id ЕРСП».

    Это копия справочника, как другие справочники ЕРСП: людьми не правится,
    загружается командой load_ersp_agencies (integrations/agencies.py),
    хранится как прислал ЕРСП (ответ целиком - в raw). Записи не удаляются:
    пропавшая из ответа получает отметку «нет в ЕРСП», удалённая в ЕРСП
    (кнопка «Delete», статус «Удалён») - отметку «удалён». И та и другая
    больше не предлагаются для связи: их agencyId ЕРСП не принимает.
    """

    class AgencyType(models.IntegerChoices):
        # agencyType в ЕРСП. Головной офис в ЕРСП - тоже «филиал» (1).
        BRANCH = 1, "филиал"
        AGENT = 2, "агент"

    insurance_company = models.ForeignKey(
        "companies.InsuranceCompany",
        on_delete=models.PROTECT,
        related_name="ersp_agencies",
        verbose_name="страховая компания",
    )
    environment = models.CharField("среда", max_length=16, choices=Environment.choices)
    ersp_id = models.PositiveIntegerField("agencyId", help_text="id записи в ЕРСП - уходит в запросы.")
    name = models.CharField("название", max_length=500)
    inn = models.CharField("ИНН", max_length=20, blank=True)
    pinfl = models.CharField("ПИНФЛ", max_length=20, blank=True)
    agency_type = models.PositiveSmallIntegerField(
        "вид в ЕРСП", choices=AgencyType.choices, null=True, blank=True,
        help_text="Как запись заведена в ЕРСП. Бывают ошибки ввода: агент, заведённый филиалом.",
    )
    parent_ersp_id = models.PositiveIntegerField(
        "входит в (agencyId)", null=True, blank=True,
        help_text="parentId в ЕРСП: головной офис, филиал или головной электронный агент. У головного офиса пусто.",
    )
    is_deleted = models.BooleanField(
        "удалён в ЕРСП", default=False,
        help_text="Отметка удаления ЕРСП: такой agencyId в запросах больше не принимается.",
    )
    is_present = models.BooleanField(
        "есть в ЕРСП", default=True, help_text="Была ли запись в последней загрузке справочника."
    )
    raw = models.JSONField("ответ ЕРСП", help_text="Строка ответа ЕРСП целиком, как пришла.")
    synced_at = models.DateTimeField(
        "обновлено из ЕРСП", help_text="Когда запись последний раз создана или изменена загрузкой."
    )

    class Meta:
        verbose_name = "запись справочника «Агенты/филиалы» ЕРСП"
        verbose_name_plural = "агенты и филиалы в ЕРСП"
        ordering = ["insurance_company__name", "environment", "ersp_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["insurance_company", "environment", "ersp_id"],
                name="integrations_erspagency_unique_id",
                violation_error_message="Эта запись ЕРСП уже есть в справочнике страховой.",
            ),
            models.CheckConstraint(
                condition=Q(environment__in=["prod", "sandbox"]),
                name="integrations_erspagency_environment_known",
            ),
        ]

    def __str__(self):
        return f"{self.ersp_id} - {self.name}"

    def can_be_linked(self):
        """Можно ли связать с нашей записью: есть в ЕРСП и не удалена там."""
        return self.is_present and not self.is_deleted
