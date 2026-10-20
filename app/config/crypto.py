"""
Шифрование секретных значений, которые хранятся в базе
(сейчас - client_secret и пароль для входа в ЕРСП).

Зачем. Резервные копии базы лежат на диске и со временем уедут на другую
площадку. Если в базе лежит пароль открытым текстом, он уезжает вместе
с копией. Поэтому в базе - только шифр, а ключ - в секрете Podman
core_encryption_key (файл /run/secrets/core_encryption_key в контейнере).
Копия базы без ключа бесполезна.

Как устроено (решение Бекзода 20.10):
- алгоритм AES-GCM (библиотека cryptography), ключ 32 байта;
- к каждому шифру «привязан» его адрес - контекст: таблица, запись, поле
  (например, "integrations.ErspCredentials:3:prod:password"). Расшифровать
  можно только с тем же контекстом. Если кто-то скопирует шифр пароля
  одной страховой в строку другой, расшифровка откажет, а не выдаст
  чужой пароль;
- шифр начинается с версии ключа ("v1:"): задел на будущую смену ключа
  (новый ключ - "v2:", старые шифры перешифровываются командой);
- одно и то же значение каждый раз шифруется по-разному (случайное
  начальное число - nonce), поэтому по шифру нельзя понять, совпадают ли
  пароли у двух записей.

Копии ключа нет нигде (решение Бекзода 20.10): ключ создаётся сразу внутри
секрета скриптом bin/encryption-key и на экран не выводится. Если ключ
потерян (например, вместе с ВМ), зашифрованные значения вводятся заново
через экран - их всегда можно получить у НАПП.

Пример:
    cipher = encrypt("пароль", context="integrations.ErspCredentials:3:prod:password")
    plain = decrypt(cipher, context="integrations.ErspCredentials:3:prod:password")
"""
import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

# Версия ключа - первая часть шифра. Сейчас ключ один.
KEY_VERSION = "v1"
KEY_SECRET = "core_encryption_key"
KEY_BYTES = 32
NONCE_BYTES = 12  # стандартная длина для AES-GCM

# Как выглядит шифр: версия, двоеточие, base64 (URL-вариант) без пробелов.
# То же правило проверяет база (CheckConstraint в моделях с шифром).
CIPHER_REGEX = r"^v[0-9]+:[A-Za-z0-9_-]+=*$"


class CryptoError(Exception):
    """Зашифровать или расшифровать не удалось. message - что показать человеку."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _key():
    """
    Ключ из секрета Podman. Читается при каждом обращении, а не при запуске
    приложения: сборка образа и автотесты без ключа не ломаются, ошибка
    появляется только там, где ключ действительно нужен.
    """
    path = settings.SECRETS_DIR / KEY_SECRET
    if not path.is_file():
        raise CryptoError(
            f"Нет ключа шифрования ({KEY_SECRET}). Его заводят на сервере командой "
            f"bin/encryption-key, а в контейнер передают строкой Secret= в quadlet/core-web.container."
        )
    try:
        key = base64.urlsafe_b64decode(path.read_text(encoding="ascii").strip())
    except (ValueError, binascii.Error):
        key = b""
    if len(key) != KEY_BYTES:
        raise CryptoError(f"Ключ шифрования {KEY_SECRET} испорчен: нужно {KEY_BYTES} байта в base64.")
    return key


def encrypt(plain, context):
    """Зашифровать строку. context - адрес значения (таблица, запись, поле)."""
    if not isinstance(plain, str) or plain == "":
        raise ValueError("Шифруется только непустая строка.")
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(_key()).encrypt(nonce, plain.encode("utf-8"), context.encode("utf-8"))
    return f"{KEY_VERSION}:{base64.urlsafe_b64encode(nonce + sealed).decode('ascii')}"


def decrypt(cipher, context):
    """Расшифровать строку, зашифрованную encrypt() с тем же context."""
    if not isinstance(cipher, str) or not re.fullmatch(CIPHER_REGEX, cipher):
        raise CryptoError("Значение в базе - не шифр. Введите его заново.")
    version, _, body = cipher.partition(":")
    if version != KEY_VERSION:
        raise CryptoError(f"Значение зашифровано ключом {version}, а в системе ключ {KEY_VERSION}.")
    try:
        raw = base64.urlsafe_b64decode(body)
    except (ValueError, binascii.Error):
        raw = b""
    if len(raw) <= NONCE_BYTES:
        raise CryptoError("Шифр в базе испорчен. Введите значение заново.")
    try:
        plain = AESGCM(_key()).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], context.encode("utf-8"))
    except InvalidTag:
        # Подпись шифра не сошлась: ключ не тот (ключ заменили или потеряли),
        # шифр испорчен или скопирован из другой записи.
        raise CryptoError(
            "Значение не расшифровывается: ключ шифрования не тот или значение "
            "скопировано из другой записи. Введите его заново."
        )
    return plain.decode("utf-8")
