import os
import sys
from pathlib import Path

# Enforced again in manage.py; keeps Django from loading user site-packages.
if not str(Path(sys.executable).resolve()).startswith("/nix/store/"):
    raise RuntimeError(
        "Django must run under Nix Python. Use: ./run.sh or nix develop devops && librelane-web"
    )

BASE_DIR = Path(__file__).resolve().parent.parent


def _project_root() -> Path:
    return Path(os.environ.get("LIBRELANE_WEB_ROOT", BASE_DIR.parent))


def _data_dir() -> Path:
    """Writable directory for SQLite DB and flow run artifacts."""
    if data := os.environ.get("LIBRELANE_DATA_DIR"):
        return Path(data).expanduser()
    root = _project_root()
    if str(root).startswith("/nix/store/"):
        return Path.home() / ".local/share/librelane-web"
    return root / ".librelane-data"


PROJECT_ROOT = _project_root()
DESIGNS_DIR = PROJECT_ROOT / "designs"
DATA_DIR = _data_dir()
RUNS_DIR = DATA_DIR

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "librelane-web-dev-only-change-in-production",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
# Default "*" for dev so the server works on localhost and public/LAN IPs.
# Override: DJANGO_ALLOWED_HOSTS=example.com,1.2.3.4
_allowed = os.environ.get("DJANGO_ALLOWED_HOSTS", "*")
ALLOWED_HOSTS = ["*"] if _allowed.strip() == "*" else [h.strip() for h in _allowed.split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "flow",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
]

ROOT_URLCONF = "librelane_web.urls"
WSGI_APPLICATION = "librelane_web.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "static/"
STATIC_ROOT = PROJECT_ROOT / "frontend" / "staticfiles"
STATICFILES_DIRS = [PROJECT_ROOT / "frontend" / "static"]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [PROJECT_ROOT / "frontend" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Notebook defaults
LIBRELANE_DESIGN_NAME = "spm"
LIBRELANE_PDK = os.environ.get("LIBRELANE_PDK", "sky130A")
LIBRELANE_PDK_FAMILY = os.environ.get("LIBRELANE_PDK_FAMILY", "sky130")
PDK_ROOT = os.path.expanduser(os.environ.get("PDK_ROOT", "~/.ciel"))


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
