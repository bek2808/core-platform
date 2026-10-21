"""
Предел размера запроса: слишком большой запрос отклоняется до разбора.

Без этого любой запрос с файлом (а Django разбирает тело ещё до проверки
входа и защиты от подделки запроса) мог бы занять память и диск контейнера.
Предел - settings.MAX_REQUEST_BODY (20 МБ: самый большой файл 15 МБ плюс поля
формы); точный предел для каждого файла проверяет files.storage.store.
"""
from django.conf import settings
from django.http import HttpResponse


class RequestSizeLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > settings.MAX_REQUEST_BODY:
            return HttpResponse(
                "Слишком большой запрос: файл больше допустимого. Уменьшите его и загрузите снова.",
                status=413, content_type="text/plain; charset=utf-8",
            )
        return self.get_response(request)
