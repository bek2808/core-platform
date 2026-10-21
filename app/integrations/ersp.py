"""
Клиент ЕРСП: вход (временный токен) и запросы к API, с записью в журнал обмена.

Как устроен вход (решения Бекзода 20.10):
- данные для входа (client_id, client_secret, логин, пароль) у каждой
  страховой свои: их вводит администратор страховой на экране
  «Интеграции -> Подключение к ЕРСП», в базе они лежат зашифрованными
  (integrations.models.ErspCredentials, docs/integrations-model.md);
- токен клиент получает сам методом /oauth/v2/token. Он один на всё
  приложение для каждой пары «страховая + среда»: процессы gunicorn делят
  его через файл в /tmp контейнера. Контейнер работает «только для чтения», и Podman
  монтирует /tmp как tmpfs - это оперативная память, а не диск. Поэтому
  токена нет ни в базе, ни в журнале, ни в резервных копиях, а после
  перезапуска контейнера он исчезает (первый запрос входит заново);
- новый токен берётся, когда старый подходит к концу (expires_in минус
  запас) или когда ЕРСП ответил 401 «не авторизован» - тогда запрос
  повторяется один раз.
- Наблюдение 20.10: ЕРСП не отзывает старые токены при выдаче новых и, похоже,
  не проверяет срок (токен Бекзода из Swagger работает с июля). Значит,
  любой выданный токен стоит считать бессрочным паролем - тем важнее, что
  он не попадает на диск.

Среда - по группе операций (решение Бекзода 20.10), см. environment_for():
справочники и провайдер - всегда боевая среда; отправка полисов - та,
что выбрана в карточке подключения страховой (по умолчанию песочница).
Клиент для операции: ErspClient.for_purpose(PROVIDER, company, user).
Явная среда (ErspClient("sandbox", company)) - для проверки входа.

Страховая обязательна: у каждой свои данные для входа, и чужих она
не получит ни при каких настройках.

Каждый запрос пишется в журнал обмена (integrations.models.Exchange), и
успешный, и ошибочный. Запись журнала идёт в своей транзакции: вызывайте
клиент вне transaction.atomic() - иначе при откате пропадёт и запись
о запросе.

Пример:
    client = ErspClient.for_purpose(PROVIDER, company=card.insurance_company, user=user)
    try:
        organization = client.provider_inn("207078596")
    except ErspError as error:
        ... error.message - что показать сотруднику; error.exchange - строка журнала
"""
import fcntl
import json
import logging
import os
import ssl
import threading
import time
import uuid
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

from config.crypto import CryptoError

from .models import Environment, ErspConnection, ErspCredentials, Exchange

# Ключи, значения которых в журнал не пишутся (заменяются на ***).
SECRET_KEYS = {"client_secret", "password", "access_token", "refresh_token"}
MASK = "***"

# Токен считаем истёкшим за 60 с до конца срока: запрос, начатый на последней
# секунде, не должен упасть посреди пути.
TOKEN_MARGIN_S = 60

# Ответ не в JSON (например, страница ошибки веб-сервера) - храним начало.
TEXT_LIMIT = 20_000


class ErspError(Exception):
    """
    Запрос к ЕРСП не удался. message - что показать человеку,
    exchange - строка журнала обмена (None, если до запроса не дошло).
    """

    def __init__(self, message, exchange=None):
        super().__init__(message)
        self.message = message
        self.exchange = exchange


class ErspConfigError(ErspError):
    """Неверная настройка: нет данных для входа, неизвестная среда."""


# Группы операций: от группы зависит среда (решение Бекзода 20.10).
REFERENCES = "references"  # справочники ЕРСП
PROVIDER = "provider"      # провайдер: организация по ИНН и др.
SEND = "send"              # отправка полисов
PURPOSES = (REFERENCES, PROVIDER, SEND)

# Где сотрудник заполняет данные для входа - для текста ошибок.
SCREEN = "Интеграции -> Подключение к ЕРСП"


def mask(value):
    """Копия JSON-значения, в которой секреты заменены на ***."""
    if isinstance(value, dict):
        return {k: MASK if k in SECRET_KEYS and v not in (None, "") else mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def is_error(payload):
    """
    Сообщает ли ответ ЕРСП об ошибке. Успех - "error": 0. В описании
    метода токена error - строка, поэтому успехом считаются и "0", и пусто.
    """
    return payload.get("error") not in (0, "0", "", None, False)


def token_result(payload):
    """
    Сведения о токене из ответа /oauth/v2/token. По описанию метода они
    в "result"; на случай стандартного ответа OAuth (поля сверху) понимаем и его.
    """
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


# --- Среда и данные для входа -------------------------------------------------------

def environment_for(purpose, company):
    """
    Среда ЕРСП для группы операций. Справочники и провайдер - боевая
    (в песочнице справочники неполные); меняется только коммитом.
    Отправка полисов - из карточки подключения страховой.
    """
    if purpose in (REFERENCES, PROVIDER):
        return Environment.PROD
    if purpose == SEND:
        connection = ErspConnection.objects.filter(insurance_company=company).first()
        return connection.send_environment if connection else Environment.SANDBOX
    raise ErspConfigError(f"Неизвестная группа операций ЕРСП: {purpose!r}.")


def load_credentials(company, environment):
    """
    Данные для входа страховой в среду - из карточки подключения
    (секреты расшифрованы). Только для запроса токена: никуда не сохранять.
    """
    label = Environment(environment).label
    row = ErspCredentials.objects.select_related("connection").filter(
        connection__insurance_company=company, environment=environment
    ).first()
    if row is None:
        raise ErspConfigError(
            f"У страховой «{company}» не заполнено подключение к ЕРСП ({label} среда). "
            f"Заполните: {SCREEN}."
        )
    missing = row.missing()
    if missing:
        raise ErspConfigError(
            f"Подключение к ЕРСП ({label} среда) заполнено не полностью, нет: "
            f"{', '.join(missing)}. Заполните: {SCREEN}."
        )
    try:
        return row.login_data()
    except CryptoError as error:
        raise ErspConfigError(f"Данные для входа в ЕРСП ({label} среда): {error.message}")


def token_name(company, environment):
    """Имя токена в общем хранилище: отдельно для каждой страховой и среды."""
    return f"{company.code}.{environment}"


# --- Общий токен для всех процессов ----------------------------------------------

logger = logging.getLogger(__name__)


class FileTokenStore:
    """
    Токены в файлах папки settings.ERSP_TOKEN_DIR (в контейнере - /tmp,
    оперативная память). Один файл на страховую и среду (mosaic.prod.json):
    токен, до какого времени им пользуемся (valid_until), когда получен
    и до какого времени действует по словам ЕРСП (для экрана).
    Права: папка 0700, файлы 0600 - читать может только пользователь приложения.

    locked(name) - блокировка файлом (flock): пока один процесс входит в ЕРСП,
    остальные ждут и потом берут уже полученный токен, а не входят сами.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)

    def _file(self, name, suffix):
        return self.directory / f"{name}{suffix}"

    @contextmanager
    def locked(self, name):
        fd = os.open(self._file(name, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _data(self, name):
        try:
            data = json.loads(self._file(name, ".json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("token") else None
        except (OSError, ValueError):
            return None

    def read(self, name):
        data = self._data(name)
        return (data["token"], data.get("valid_until")) if data else None

    def info(self, name):
        """Сведения о токене без самого токена: когда получен, до какого времени действует."""
        data = self._data(name)
        return {k: data.get(k) for k in ("obtained_at", "expires_at")} if data else None

    def write(self, name, token, valid_until, obtained_at=None, expires_at=None):
        # Сначала во временный файл, потом подмена: соседний процесс никогда
        # не прочитает наполовину записанный файл.
        temp = self._file(name, f".{os.getpid()}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"token": token, "valid_until": valid_until,
                       "obtained_at": obtained_at, "expires_at": expires_at}, file)
        os.replace(temp, self._file(name, ".json"))

    def forget(self, name):
        self._file(name, ".json").unlink(missing_ok=True)

    def drop(self, name, token):
        """Стереть токен, но только если это тот, который отверг ЕРСП."""
        current = self.read(name)
        if current is not None and current[0] == token:
            self._file(name, ".json").unlink(missing_ok=True)

    def clear(self):
        for path in self.directory.glob("*.json"):
            path.unlink(missing_ok=True)


class MemoryTokenStore:
    """Запасной вариант, если папку для токенов создать нельзя: память процесса."""

    def __init__(self):
        self.tokens = {}
        self.lock = threading.RLock()

    @contextmanager
    def locked(self, name):
        with self.lock:
            yield

    def read(self, name):
        data = self.tokens.get(name)
        return (data["token"], data["valid_until"]) if data else None

    def info(self, name):
        data = self.tokens.get(name)
        return {k: data[k] for k in ("obtained_at", "expires_at")} if data else None

    def write(self, name, token, valid_until, obtained_at=None, expires_at=None):
        self.tokens[name] = {"token": token, "valid_until": valid_until,
                             "obtained_at": obtained_at, "expires_at": expires_at}

    def drop(self, name, token):
        if (self.tokens.get(name) or {}).get("token") == token:
            self.tokens.pop(name, None)

    def forget(self, name):
        self.tokens.pop(name, None)

    def clear(self):
        self.tokens.clear()


_memory_store = MemoryTokenStore()


def token_store():
    """Общее хранилище; если папка недоступна - память процесса (с записью в журнал)."""
    try:
        return FileTokenStore(settings.ERSP_TOKEN_DIR)
    except OSError as error:
        logger.warning("Папка токенов ЕРСП недоступна (%s) - токен только в памяти процесса", error)
        return _memory_store


def forget_tokens():
    """Забыть все токены (для тестов)."""
    _memory_store.clear()
    store = token_store()
    store.clear()


def forget_token(company, environment):
    """
    Забыть токен страховой в среде - после смены данных для входа: следующий
    запрос войдёт заново уже с новыми данными (и сразу покажет, если они неверны).
    """
    store = token_store()
    name = token_name(company, environment)
    with store.locked(name):
        store.forget(name)


def token_status(company, environment):
    """
    Состояние токена для экрана - без самого токена: {"obtained_at",
    "expires_at"} (секунды Unix; expires_at - None, если ЕРСП срок не назвал)
    или None, если токена нет (не было входа или контейнер перезапускался).
    """
    return token_store().info(token_name(company, environment))


# --- Отправка HTTP-запроса --------------------------------------------------------

def urllib_transport(method, url, headers, body, timeout):
    """
    Отправить запрос и вернуть (код HTTP, тело ответа в байтах).
    Сертификат сервера проверяется всегда (стандартные настройки Python).
    Ответы 4xx и 5xx возвращаются как обычный ответ - разбирает их клиент.
    Нет связи, тайм-аут, ошибка сертификата - исключение OSError.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class ErspClient:
    def __init__(self, environment, company, user=None, transport=None, clock=time.time):
        if environment not in Environment.values:
            raise ErspConfigError(f"Неизвестная среда ЕРСП: {environment!r} (нужно prod или sandbox).")
        if company is None:
            raise ErspConfigError(
                "Не указана страховая компания: данные для входа в ЕРСП у каждой страховой свои "
                "(в учётной записи сотрудника должна быть страховая)."
            )
        self.environment = environment
        self.company = company
        self.user = user
        self.base_url = settings.ERSP_URLS[environment].rstrip("/")
        self.transport = transport or urllib_transport
        self.clock = clock  # обычные часы: срок токена сравнивают разные процессы
        self.store = token_store()
        self.token_name = token_name(company, environment)
        # Последняя строка журнала обмена: по ней страница сверки находит
        # ответ, который видел сотрудник (и применяет именно его).
        self.last_exchange = None

    @classmethod
    def for_purpose(cls, purpose, company, user=None, **kwargs):
        """Клиент для группы операций: среду выбирает environment_for()."""
        return cls(environment_for(purpose, company), company, user=user, **kwargs)

    # --- Вход ---------------------------------------------------------------------

    def token_info(self):
        """
        Войти в ЕРСП (всегда новый вход) и вернуть сведения о токене без самого
        токена: тип, срок жизни, область. Для проверки подключения (кнопка на
        экране, команда ersp_check). Новый токен становится общим токеном.
        """
        with self.store.locked(self.token_name):
            result = self._login()
        return {k: result.get(k) for k in ("token_type", "expires_in", "scope")}

    def _token(self):
        """Общий токен; если его нет или срок вышел - войти (один процесс за раз)."""
        with self.store.locked(self.token_name):
            cached = self.store.read(self.token_name)
            if cached is not None:
                token, valid_until = cached
                if valid_until is None or self.clock() < valid_until:
                    return token
            return self._login()["access_token"]

    def _login(self):
        """Вход в ЕРСП. Вызывается под блокировкой store.locked()."""
        credentials = load_credentials(self.company, self.environment)
        body = {
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "grant_type": "password",
            "username": credentials["username"],
            "password": credentials["password"],
        }
        payload, _ = self._send(Exchange.Operation.TOKEN, "/oauth/v2/token", body, authorized=False)
        result = token_result(payload)
        token = result["access_token"]  # наличие проверил _send
        expires_in = result.get("expires_in")
        obtained_at = self.clock()
        valid_until = expires_at = None
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
            expires_at = obtained_at + expires_in
            valid_until = obtained_at + max(expires_in - TOKEN_MARGIN_S, 0)
        self.store.write(self.token_name, token, valid_until, obtained_at=obtained_at, expires_at=expires_at)
        return result

    # --- Запросы к API --------------------------------------------------------------

    def provider_inn(self, inn):
        """
        Сведения об организации Узбекистана по ИНН (метод провайдера
        /api/provider/inn). Возвращает словарь "result" из ответа ЕРСП;
        ответ целиком - в журнале обмена.
        """
        payload, _ = self._call(Exchange.Operation.PROVIDER_INN, "/api/provider/inn", {"inn": inn}, subject=inn)
        return payload["result"]

    def provider_passport(self, document, birth_date):
        """
        Сведения о физлице по паспорту и дате рождения (метод провайдера
        /api/provider/passport-birth-date-v2). document - серия и номер
        слитно («AD0000000»), birth_date - дата. Возвращает словарь "result".
        """
        body = self._person_request({"document": document, "birthDate": birth_date.isoformat()})
        payload, _ = self._call(
            Exchange.Operation.PROVIDER_PASSPORT, "/api/provider/passport-birth-date-v2", body, subject=document,
        )
        return payload["result"]

    def provider_pinfl(self, pinfl):
        """
        Сведения о физлице по ПИНФЛ (метод провайдера /api/provider/pinfl-v2).
        Возвращает словарь "result".
        """
        body = self._person_request({"pinfl": pinfl})
        payload, _ = self._call(
            Exchange.Operation.PROVIDER_PINFL, "/api/provider/pinfl-v2", body, subject=pinfl,
        )
        return payload["result"]

    def _person_request(self, fields):
        """
        Тело запроса о физлице: общие поля провайдера + свои. senderPinfl -
        ПИНФЛ того, кто запрашивает (решение Бекзода 21.10): берётся из учётной
        записи сотрудника. Согласие isConsent - «Y»: запрашивает сотрудник страховой
        по паспортным данным из подписанного соглашения.
        """
        sender = getattr(self.user, "pinfl", "") if self.user is not None else ""
        if not sender:
            raise ErspError(
                "В вашей учётной записи не заполнен ПИНФЛ: ЕРСП требует указать, кто запрашивает "
                "данные физлица. Заполните ПИНФЛ в карточке пользователя."
            )
        return {"transactionId": uuid.uuid4().hex, "isConsent": "Y", "senderPinfl": sender, **fields}

    def references_agencies(self):
        """
        Справочник «Агенты/филиалы» своей страховой (GET /api/references/agencies):
        головной офис, филиалы и агенты с их agencyId. Возвращает список строк
        "result"; ответ целиком - в журнале обмена. id в песочнице и в боевой
        среде разные - берите клиент той среды, куда уходят полисы (SEND).
        """
        payload, _ = self._call(
            Exchange.Operation.REFERENCES_AGENCIES, "/api/references/agencies", None,
            method="GET", result_type=list,
        )
        return payload["result"]

    def _call(self, operation, path, body, subject="", **send_options):
        """Запрос с токеном; на 401 - новый вход и один повтор."""
        token = self._token()
        try:
            return self._send(operation, path, body, subject=subject, token=token, **send_options)
        except _Unauthorized:
            with self.store.locked(self.token_name):
                # Стираем только отвергнутый токен: если соседний процесс уже
                # вошёл заново, его свежий токен остаётся.
                self.store.drop(self.token_name, token)
            try:
                return self._send(operation, path, body, subject=subject, token=self._token(), **send_options)
            except _Unauthorized as again:
                self._fail(
                    again.exchange, None,
                    "ЕРСП не принимает вход (401) даже с новым токеном. "
                    f"Проверьте данные для входа: {SCREEN}.",
                )

    def _send(self, operation, path, body, subject="", token=None, authorized=True,
              method="POST", result_type=dict):
        """
        Отправить запрос, записать его в журнал обмена и разобрать ответ.
        method - POST (тело - JSON) или GET (тела нет, body=None: справочники).
        result_type - каким должен быть "result" в ответе: dict (одна запись)
        или list (справочник).
        Возвращает (ответ JSON, строка журнала) или бросает ErspError.
        """
        url = self.base_url + path
        headers = {"Accept": "application/json", "User-Agent": "core-platform"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if authorized:
            headers["Authorization"] = f"Bearer {token}"
        log = Exchange(
            user=self.user,
            insurance_company=self.company,
            environment=self.environment,
            operation=operation,
            subject=str(subject)[:100],
            method=method,
            url=url,
            request_body=mask(body) if body is not None else None,
        )
        self.last_exchange = log
        started = time.monotonic()
        try:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            status, raw = self.transport(method, url, headers, data, settings.ERSP_TIMEOUT_S)
        except OSError as error:  # нет связи, тайм-аут, ошибка сертификата
            log.duration_ms = int((time.monotonic() - started) * 1000)
            self._fail(log, Exchange.Outcome.NETWORK_ERROR, f"Нет связи с ЕРСП ({self.environment}): {error}")
        log.duration_ms = int((time.monotonic() - started) * 1000)
        log.http_status = status

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            payload = None
            log.response_text = raw.decode("utf-8", errors="replace")[:TEXT_LIMIT]
        if isinstance(payload, (dict, list)):
            log.response_body = mask(payload)

        if status == 401 and authorized:
            log.outcome = Exchange.Outcome.HTTP_ERROR
            log.error_message = "401: токен не принят"
            log.save()
            raise _Unauthorized(log)
        if not 200 <= status < 300:
            message = payload.get("error_message") if isinstance(payload, dict) else ""
            self._fail(log, Exchange.Outcome.HTTP_ERROR, f"ЕРСП ответил HTTP {status}. {message or ''}".strip())
        if not isinstance(payload, dict):
            self._fail(log, Exchange.Outcome.BAD_RESPONSE, "ЕРСП прислал ответ не в формате JSON.")
        if is_error(payload):
            log.error_code = str(payload.get("error"))[:50]
            self._fail(
                log, Exchange.Outcome.ERSP_ERROR,
                f"ЕРСП: {payload.get('error_message') or 'ошибка ' + log.error_code}",
            )
        # Всё проверяется ДО записи «успешно»: строку журнала потом не исправить
        # (триггер в базе запрещает правку - так задумано).
        if operation == Exchange.Operation.TOKEN:
            if not token_result(payload).get("access_token"):
                self._fail(log, Exchange.Outcome.BAD_RESPONSE, "В ответе ЕРСП нет токена (access_token).")
        elif not isinstance(payload.get("result"), result_type):
            self._fail(log, Exchange.Outcome.BAD_RESPONSE, "В ответе ЕРСП нет данных (result).")

        log.outcome = Exchange.Outcome.OK
        log.save()
        return payload, log

    @staticmethod
    def _fail(log, outcome, message):
        if outcome is not None:
            log.outcome = outcome
            log.error_message = message
            log.save()
        raise ErspError(message, exchange=log)


class _Unauthorized(Exception):
    def __init__(self, exchange):
        self.exchange = exchange

