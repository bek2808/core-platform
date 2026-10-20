"""
Автотесты общих вещей Core-системы: проверка живости, заголовки защиты,
работа с базой.

Запуск на сервере: bin/test (подробнее - в AGENTS.md).
"""
from django.db import connection
from django.test import SimpleTestCase, TestCase

from .testing import refusal_expected


class HealthzTests(SimpleTestCase):
    """Адрес /healthz/: по нему Podman каждые 30 секунд проверяет, живо ли приложение."""

    def test_answers_ok(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_only_get_allowed(self):
        # Проверка живости только читает; любой другой метод - ошибка 405.
        with refusal_expected(self):
            response = self.client.post("/healthz/")
        self.assertEqual(response.status_code, 405)


class SecurityHeadersTests(SimpleTestCase):
    """Заголовки защиты, которые браузер получает с каждым ответом."""

    def test_headers_present(self):
        response = self.client.get("/healthz/")
        # Страницу нельзя встроить в чужой сайт (защита от подмены кликов).
        self.assertEqual(response["X-Frame-Options"], "DENY")
        # Браузер не должен угадывать тип файла (защита от подмены содержимого).
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        # Адрес наших страниц не уходит на сторонние сайты.
        self.assertEqual(response["Referrer-Policy"], "same-origin")


class DatabaseTests(TestCase):
    """Подключение к базе во время тестов."""

    def test_runs_on_separate_test_database(self):
        # Тесты создают и стирают данные, поэтому они никогда не должны
        # работать в рабочей базе core - только во временной test_*.
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            name = cursor.fetchone()[0]
        self.assertTrue(name.startswith("test_"), f"тесты подключились к базе {name}")

    def test_connection_time_zone_is_utc(self):
        # Правило: время в базе - UTC, показ пользователю - по его поясу.
        with connection.cursor() as cursor:
            cursor.execute("SHOW TIME ZONE")
            self.assertEqual(cursor.fetchone()[0], "UTC")
