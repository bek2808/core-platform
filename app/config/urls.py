"""Адреса (URL) приложения."""
from django.contrib import admin
from django.http import HttpResponse
from django.urls import path
from django.views.decorators.http import require_GET


@require_GET
def healthz(request):
    """Проверка живости для Podman: приложение запущено и отвечает."""
    return HttpResponse("ok", content_type="text/plain")


admin.site.site_header = "Core - страховая система"
admin.site.site_title = "Core"
admin.site.index_title = "Администрирование"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", healthz),
]
