"""
Общие помощники автотестов.
"""
from contextlib import contextmanager


@contextmanager
def refusal_expected(test):
    """
    Ожидаемый отказ: 403 «нет прав», 405 «такой запрос не разрешён».

    Django записывает каждый отказ в журнал приложения - с полной
    трассировкой, и в выводе тестов это выглядело как ошибка, хотя тест
    проходил. Здесь запись перехватывается (в вывод не попадает), а заодно
    проверяется, что отказ действительно был записан в журнал.

        with refusal_expected(self):
            response = self.client.post(url_удаления)
        self.assertEqual(response.status_code, 403)
    """
    with test.assertLogs("django.request", level="WARNING"):
        yield
