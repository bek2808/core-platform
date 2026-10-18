"""
Настройки Django для Core-системы.

Правило: в этом файле нет ни паролей, ни адресов конкретного сервера.
- Пароли и ключи читаются из секретов Podman: файлы /run/secrets/<имя>.
- Всё, что зависит от среды (разработка, тест, промышленная), - из переменных
  окружения DJANGO_*, которые задаются в quadlet/core-web.container.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))


def env(name, default=None):
    """Значение переменной окружения или default."""
    return os.environ.get(name, default)


def env_bool(name, default=False):
    """Переменная окружения как да/нет: 1, true, yes, on - это да."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    """Список через запятую: "a, b" -> ["a", "b"]."""
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


def read_secret(name, env_fallback=None, required=True):
    """
    Секрет из файла /run/secrets/<name> (так Podman передаёт секреты).
    env_fallback - имя переменной окружения на случай, когда файла нет
    (например, при сборке образа). Значение никогда не выводится в журнал.
    """
    path = SECRETS_DIR / name
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    if env_fallback and env_fallback in os.environ:
        return os.environ[env_fallback]
    if required:
        raise ImproperlyConfigured(f"Не найден секрет {name} ({path})")
    return ""


# --- Безопасность -----------------------------------------------------------

SECRET_KEY = read_secret("django_secret_key", env_fallback="DJANGO_SECRET_KEY")

# DEBUG выключен по умолчанию: в промышленной среде он раскрывает внутренности.
DEBUG = env_bool("DJANGO_DEBUG", False)

# Адреса (имена), по которым разрешено обращаться к приложению.
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Куки только по HTTPS. Пока HTTPS нет (вход через SSH-туннель) - выключено;
# включается переменной DJANGO_SECURE_COOKIES=true на этапе HTTPS.
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", False)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 8 * 60 * 60          # сессия живёт 8 часов (рабочий день)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"


# --- Приложения ---------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Наши приложения
    "accounts",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# --- База данных ----------------------------------------------------------------
# Подключение под ролью core_app (не суперпользователь) по сети core-net.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env("DJANGO_DB_HOST", "core-postgres"),
        "PORT": env("DJANGO_DB_PORT", "5432"),
        "NAME": env("DJANGO_DB_NAME", "core"),
        "USER": env("DJANGO_DB_USER", "core_app"),
        "PASSWORD": read_secret("pg_app_password", required=False),
        # Держать соединение до 60 с, а не открывать новое на каждый запрос;
        # перед использованием проверять, что оно живо (например, после перезапуска базы).
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"application_name": "core-web"},
    }
}

# Пользователь - наша модель accounts.User. Задаётся один раз, до первой миграции:
# позже поменять модель пользователя очень трудно.
AUTH_USER_MODEL = "accounts.User"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Пароли пользователей ---------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --- Язык и время -----------------------------------------------------------------
# В базе время хранится в UTC (timestamptz), пользователям показывается по Ташкенту.

LANGUAGE_CODE = "ru"
LANGUAGES = [
    ("ru", "Русский"),
    ("uz", "Oʻzbekcha"),
    ("en", "English"),
]
TIME_ZONE = env("DJANGO_TIME_ZONE", "Asia/Tashkent")
USE_I18N = True
USE_TZ = True


# --- Статические файлы (CSS, JS, картинки админ-панели) ---------------------------
# Собираются в образ при сборке (collectstatic), раздаются через whitenoise.

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}


# --- Журналы ----------------------------------------------------------------------
# Всё пишется в стандартный вывод: его собирает journald, смотреть через
# bin/core-run podman logs core-web

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
}
