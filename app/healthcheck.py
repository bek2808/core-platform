"""
Проверка живости для Podman (HealthCmd в quadlet/core-web.container).
Код 0 - приложение отвечает на /healthz/, код 1 - нет.
Отдельный файл, а не python -c "...": кавычки в Quadlet-файлах легко ломаются.
"""
import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/healthz/", timeout=5) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
