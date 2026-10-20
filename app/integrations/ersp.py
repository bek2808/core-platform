"""
Клиент ЕРСП: вход (временный токен) и запросы к API, с записью в журнал обмена.

Как устроен вход (решение Бекзода 20.10):
- в секретах Podman лежат данные для входа, а не токен:
  ersp_prod_credentials и ersp_sandbox_credentials (JSON с client_id,
  client_secret, username, password; заводятся скриптом bin/ersp-secret);
- токен клиент получает сам методом /oauth/v2/token. Он один на всё
  приложение (решение Бекзода 20.10): процессы gunicorn делят его через файл
  в /tmp контейнера. Контейнер работает «только для чтения», и Podman
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

Среда задаётся явно: environment = "prod" или "sandbox". Какая среда для
чего - в настройках: ERSP_READ_ENV (чтение: провайдер, справочники)
и ERSP_SEND_ENV (отправка полисов).

Данные для входа выбираются по страховой компании (мультиарендность):
- секрет ersp_<среда>_credentials_<код страховой>, если он есть;
- иначе общий ersp_<среда>_credentials - но только для страховой, которой
  он принадлежит (настройка ERSP_CREDENTIALS_COMPANY, сейчас Mosaic).
Чужая страховая без своего секрета получает понятную ошибку, а не данные
для входа Mosaic.

Каждый запрос пишется в журнал обмена (integrations.models.Exchange), и
успешный, и ошибочный. Запись журнала идёт в своей транзакции: вызывайте
клиент вне transaction.atomic() - иначе при откате пропадёт и запись
о запросе.

Пример:
    client = ErspClient(settings.ERSP_READ_ENV, company=user.insurance_company, user=user)
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
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

from .models import Environment, Exchange

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


# --- Данные для входа --------------------------------------------------------------

def credentials_secret(environment, company=None):
    """Имя секрета с данными для входа этой страховой в эту среду."""
    owner = settings.ERSP_CREDENTIALS_COMPANY
    code = company.code if company is not None else owner
    own = f"ersp_{environment}_credentials_{code}"
    if (settings.SECRETS_DIR / own).is_file():
        return own
    if code == owner:
        return f"ersp_{environment}_credentials"
    raise ErspConfigError(
        f"Нет данных для входа в ЕРСП у страховой «{company}» (среда {environment}): "
        f"нужен секрет {own}."
    )


def read_credentials(secret_name):
    path = settings.SECRETS_DIR / secret_name
    if not path.is_file():
        raise ErspConfigError(
            f"Не найден секрет {secret_name}. Его заводят на сервере командой "
            f"bin/ersp-secret, а в контейнер передают строкой Secret= в quadlet/core-web.container."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise ErspConfigError(f"Секрет {secret_name} - не JSON. Заведите его заново: bin/ersp-secret --replace.")
    missing = [k for k in ("client_id", "client_secret", "username", "password") if not data.get(k)]
    if missing:
        raise ErspConfigError(f"В секрете {secret_name} нет значений: {', '.join(missing)}.")
    return data


# --- Общий токен для всех процессов ----------------------------------------------

logger = logging.getLogger(__name__)


class FileTokenStore:
    """
    Токены в файлах папки settings.ERSP_TOKEN_DIR (в контейнере - /tmp,
    оперативная память). Один файл на секрет: {"token": ..., "valid_until": ...}.
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

    def read(self, name):
        try:
            data = json.loads(self._file(name, ".json").read_text(encoding="utf-8"))
            return data["token"], data.get("valid_until")
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def write(self, name, token, valid_until):
        # Сначала во временный файл, потом подмена: соседний процесс никогда
        # не прочитает наполовину записанный файл.
        temp = self._file(name, f".{os.getpid()}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"token": token, "valid_until": valid_until}, file)
        os.replace(temp, self._file(name, ".json"))

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
        return self.tokens.get(name)

    def write(self, name, token, valid_until):
        self.tokens[name] = (token, valid_until)

    def drop(self, name, token):
        if self.tokens.get(name, (None,))[0] == token:
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
    """Забыть все токены (для тестов и после замены секрета)."""
    _memory_store.clear()
    store = token_store()
    store.clear()


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
    def __init__(self, environment, company=None, user=None, transport=None, clock=time.time):
        if environment not in Environment.values:
            raise ErspConfigError(f"Неизвестная среда ЕРСП: {environment!r} (нужно prod или sandbox).")
        self.environment = environment
        self.company = company
        self.user = user
        self.base_url = settings.ERSP_URLS[environment].rstrip("/")
        self.transport = transport or urllib_transport
        self.clock = clock  # обычные часы: срок токена сравнивают разные процессы
        self.store = token_store()
        self.secret_name = credentials_secret(environment, company)
        # Последняя строка журнала обмена: по ней страница сверки находит
        # ответ, который видел сотрудник (и применяет именно его).
        self.last_exchange = None

    # --- Вход ---------------------------------------------------------------------

    def token_info(self):
        """
        Войти в ЕРСП (всегда новый вход) и вернуть сведения о токене без самого
        токена: тип, срок жизни, область. Для проверки входа командой ersp_check.
        Новый токен становится общим токеном приложения.
        """
        with self.store.locked(self.secret_name):
            result = self._login()
        return {k: result.get(k) for k in ("token_type", "expires_in", "scope")}

    def _token(self):
        """Общий токен; если его нет или срок вышел - войти (один процесс за раз)."""
        with self.store.locked(self.secret_name):
            cached = self.store.read(self.secret_name)
            if cached is not None:
                token, valid_until = cached
                if valid_until is None or self.clock() < valid_until:
                    return token
            return self._login()["access_token"]

    def _login(self):
        """Вход в ЕРСП. Вызывается под блокировкой store.locked()."""
        credentials = read_credentials(self.secret_name)
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
        valid_until = None
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            valid_until = self.clock() + max(expires_in - TOKEN_MARGIN_S, 0)
        self.store.write(self.secret_name, token, valid_until)
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

    def _call(self, operation, path, body, subject=""):
        """Запрос с токеном; на 401 - новый вход и один повтор."""
        token = self._token()
        try:
            return self._send(operation, path, body, subject=subject, token=token)
        except _Unauthorized:
            with self.store.locked(self.secret_name):
                # Стираем только отвергнутый токен: если соседний процесс уже
                # вошёл заново, его свежий токен остаётся.
                self.store.drop(self.secret_name, token)
            try:
                return self._send(operation, path, body, subject=subject, token=self._token())
            except _Unauthorized as again:
                self._fail(
                    again.exchange, None,
                    "ЕРСП не принимает вход (401) даже с новым токеном. "
                    "Проверьте данные для входа: bin/ersp-secret --replace.",
                )

    def _send(self, operation, path, body, subject="", token=None, authorized=True):
        """
        Отправить запрос, записать его в журнал обмена и разобрать ответ.
        Возвращает (ответ JSON, строка журнала) или бросает ErspError.
        """
        url = self.base_url + path
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "core-platform",
        }
        if authorized:
            headers["Authorization"] = f"Bearer {token}"
        log = Exchange(
            user=self.user,
            insurance_company=self.company or _owner_company(),
            environment=self.environment,
            operation=operation,
            subject=str(subject)[:100],
            method="POST",
            url=url,
            request_body=mask(body),
        )
        self.last_exchange = log
        started = time.monotonic()
        try:
            status, raw = self.transport("POST", url, headers, json.dumps(body).encode("utf-8"), settings.ERSP_TIMEOUT_S)
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
        elif not isinstance(payload.get("result"), dict):
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


def _owner_company():
    """Страховая, которой принадлежат общие данные для входа (для журнала)."""
    from companies.models import InsuranceCompany

    return InsuranceCompany.objects.filter(code=settings.ERSP_CREDENTIALS_COMPANY).first()
