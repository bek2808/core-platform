"""
Автотесты шифрования секретных значений (config/crypto.py).

Ключ - во временной папке вместо /run/secrets (у контейнера тестов
настоящего ключа нет и быть не должно).

Запуск на сервере: bin/test config
"""
import base64
import os
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from . import crypto

CONTEXT = "integrations.ErspCredentials:1:prod:password"
PLAIN = 'пароль "с кавычками" \\ и пробелом '


def write_key(folder, key=None):
    key = key if key is not None else os.urandom(crypto.KEY_BYTES)
    (Path(folder) / crypto.KEY_SECRET).write_text(base64.urlsafe_b64encode(key).decode("ascii") + "\n")


class KeyFolderMixin:
    """Временная папка секретов с ключом шифрования."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.secrets = Path(folder.name)
        write_key(self.secrets)
        override = override_settings(SECRETS_DIR=self.secrets)
        override.enable()
        self.addCleanup(override.disable)


class CryptoTests(KeyFolderMixin, SimpleTestCase):
    def test_round_trip(self):
        cipher = crypto.encrypt(PLAIN, CONTEXT)
        self.assertEqual(crypto.decrypt(cipher, CONTEXT), PLAIN)

    def test_cipher_format_and_no_plain_text(self):
        cipher = crypto.encrypt("simple-password", CONTEXT)
        self.assertTrue(cipher.startswith("v1:"))
        self.assertRegex(cipher, crypto.CIPHER_REGEX)
        self.assertNotIn("simple-password", cipher)
        self.assertNotIn("simple-password", base64.urlsafe_b64decode(cipher[3:]).decode("latin-1"))

    def test_same_value_encrypts_differently(self):
        # По шифрам нельзя понять, что у двух записей одинаковый пароль.
        self.assertNotEqual(crypto.encrypt(PLAIN, CONTEXT), crypto.encrypt(PLAIN, CONTEXT))

    def test_other_context_refused(self):
        # Шифр, скопированный в другую запись (другая страховая или среда), не расшифровывается.
        cipher = crypto.encrypt(PLAIN, CONTEXT)
        for other in [
            "integrations.ErspCredentials:2:prod:password",
            "integrations.ErspCredentials:1:sandbox:password",
            "integrations.ErspCredentials:1:prod:client_secret",
        ]:
            with self.subTest(other=other), self.assertRaises(crypto.CryptoError):
                crypto.decrypt(cipher, other)

    def test_other_key_refused(self):
        cipher = crypto.encrypt(PLAIN, CONTEXT)
        write_key(self.secrets)  # ключ заменили
        with self.assertRaisesMessage(crypto.CryptoError, "ключ шифрования не тот"):
            crypto.decrypt(cipher, CONTEXT)

    def test_tampered_cipher_refused(self):
        cipher = crypto.encrypt(PLAIN, CONTEXT)
        raw = bytearray(base64.urlsafe_b64decode(cipher[3:]))
        raw[-1] ^= 1  # испортили один бит
        tampered = "v1:" + base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt(tampered, CONTEXT)

    def test_not_a_cipher_refused(self):
        for value in ["", "пароль", "v1:", "v1:@@@", "v1:AAAA", "v2:" + "A" * 40]:
            with self.subTest(value=value), self.assertRaises(crypto.CryptoError):
                crypto.decrypt(value, CONTEXT)

    def test_empty_value_not_encrypted(self):
        with self.assertRaises(ValueError):
            crypto.encrypt("", CONTEXT)

    def test_no_key_clear_error(self):
        (self.secrets / crypto.KEY_SECRET).unlink()
        with self.assertRaisesMessage(crypto.CryptoError, "bin/encryption-key"):
            crypto.encrypt(PLAIN, CONTEXT)

    def test_broken_key_clear_error(self):
        for content in ["не base64", base64.urlsafe_b64encode(b"short").decode("ascii")]:
            (self.secrets / crypto.KEY_SECRET).write_text(content)
            with self.subTest(content=content), self.assertRaisesMessage(crypto.CryptoError, "испорчен"):
                crypto.encrypt(PLAIN, CONTEXT)

    def test_error_message_has_no_secret(self):
        cipher = crypto.encrypt(PLAIN, CONTEXT)
        try:
            crypto.decrypt(cipher, "integrations.ErspCredentials:2:prod:password")
        except crypto.CryptoError as error:
            self.assertNotIn(PLAIN.strip(), error.message)
            self.assertNotIn(cipher, error.message)

