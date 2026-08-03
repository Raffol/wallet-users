"""Настройки проекта.

SQLite по умолчанию — Docker и PostgreSQL не нужны для запуска.
Чтобы переключиться на Postgres, задайте DATABASE_URL в .env:
    DATABASE_URL=postgres://smc:smc@localhost:5432/smc
и раскомментируйте psycopg в requirements.txt.
"""

from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- .env
def _load_env() -> dict[str, str]:
    """Минимальный читатель .env — без зависимости от сторонних библиотек."""
    values: dict[str, str] = {}
    path = BASE_DIR / ".env"
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


ENV = _load_env()


def env(key: str, default: str = "") -> str:
    import os

    return ENV.get(key) or os.environ.get(key) or default


def env_bool(key: str, default: bool = False) -> bool:
    raw = env(key, str(default)).lower()
    return raw in {"1", "true", "yes", "on"}


# --------------------------------------------------------------- базовое

SECRET_KEY = env("SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = env_bool("DEBUG", True)

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
if not DEBUG:
    ALLOWED_HOSTS += [h for h in env("ALLOWED_HOSTS").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "accounts",
    "tickets",
    "posts",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
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

# Все пути API заканчиваются слешем — конвенция Django, и фронтенд
# ей следует. APPEND_SLASH оставлен включённым как страховка: он
# выручит при опечатке в GET-ссылке, но на POST не поможет — при
# редиректе тело запроса теряется, поэтому слеши важны именно в коде.
APPEND_SLASH = True


# ----------------------------------------------------------------- база

_db_url = env("DATABASE_URL")
if _db_url:
    parsed = urlparse(_db_url)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed.path.lstrip("/"),
            "USER": parsed.username or "",
            "PASSWORD": parsed.password or "",
            "HOST": parsed.hostname or "localhost",
            "PORT": str(parsed.port or 5432),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# ------------------------------------------------------------- аккаунты

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Argon2 первым: он же используется для хеширования новых паролей
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

LOGIN_URL = "/admin/login/"


# ------------------------------------------------------------------ DRF

REST_FRAMEWORK = {
    # Сессии Django: cookie httpOnly ставится и проверяется фреймворком,
    # плюс встроенная защита от CSRF. Токены здесь не нужны — фронт и
    # бэкенд живут на одном источнике через прокси Vite.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Закрыто по умолчанию: публичные эндпоинты открываются поштучно
    # через @permission_classes([AllowAny]). Обратный порядок опаснее —
    # забытый декоратор оставляет дыру, а не лишний запрос логина.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ] + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}

# На http://localhost браузер не отправляет cookie с флагом Secure.
# Если поставить True без HTTPS, вход будет молча слетать.
SESSION_COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_HTTPONLY = True
# csrftoken читается из JavaScript — иначе axios не сможет его переслать
CSRF_COOKIE_HTTPONLY = False
SESSION_COOKIE_AGE = 60 * 60 * 8  # рабочий день

CSRF_TRUSTED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

# Нужен только если фронт обращается к бэкенду напрямую, минуя прокси Vite
CORS_ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
CORS_ALLOW_CREDENTIALS = True


# ---------------------------------------------------------------- файлы

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------- прочее

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Asia/Irkutsk"
USE_I18N = True
USE_TZ = True

# Ограничение попыток входа
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300
PUBLIC_FORM_MAX_PER_HOUR = 5
