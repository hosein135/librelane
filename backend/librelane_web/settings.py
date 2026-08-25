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
    """Writable directory for Postgres data hints and local caches."""
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
# Per-user/per-run LibreLane workspaces live here (not OS /tmp).
RUNS_ROOT = DATA_DIR / "runs"

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "librelane-web-dev-only-change-in-production",
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
# Default "*" for dev so the server works on localhost and public/LAN IPs.
_allowed = os.environ.get("DJANGO_ALLOWED_HOSTS", "*")
ALLOWED_HOSTS = ["*"] if _allowed.strip() == "*" else [h.strip() for h in _allowed.split(",") if h.strip()]

# Next.js UI origin. Django HTML page GETs redirect here; APIs stay on Django.
NEXT_ORIGIN = os.environ.get("NEXT_ORIGIN", "http://127.0.0.1:3000").rstrip("/")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "flow",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "librelane_web.urls"
WSGI_APPLICATION = "librelane_web.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ.get("PGHOST", "127.0.0.1"),
        "PORT": os.environ.get("PGPORT", "5432"),
        "NAME": os.environ.get("PGDATABASE", "theapp"),
        "USER": os.environ.get("PGUSER", "theapp"),
        "PASSWORD": os.environ.get("PGPASSWORD", "theapp"),
        "CONN_MAX_AGE": 60,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "static/"
STATIC_ROOT = PROJECT_ROOT / "frontend" / "staticfiles"
STATICFILES_DIRS = []
_legacy_static = PROJECT_ROOT / "frontend" / "static"
if _legacy_static.is_dir():
    STATICFILES_DIRS.append(_legacy_static)

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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
# Shared Ciel root for all supported PDK families (not user-configurable in the UI).
PDK_ROOT = os.path.expanduser(os.environ.get("PDK_ROOT", "~/.ciel"))
# Optional override for run workspaces; default is DATA_DIR/runs.
LIBRELANE_TEMP_ROOT = os.environ.get("LIBRELANE_TEMP_ROOT", "")

# Storage policy (disk workdirs + Postgres artifacts).
STORAGE_USER_QUOTA_BYTES = int(
    os.environ.get("LIBRELANE_USER_QUOTA_BYTES", str(20 * 1024**3))
)
STORAGE_RUN_BUDGET_BYTES = int(
    os.environ.get("LIBRELANE_RUN_BUDGET_BYTES", str(10 * 1024**3))
)
STORAGE_MIN_FREE_BYTES = int(
    os.environ.get("LIBRELANE_MIN_FREE_BYTES", str(5 * 1024**3))
)
# 0 = keep workdirs until the user deletes the run.
STORAGE_WORKDIR_RETENTION_DAYS = int(
    os.environ.get("LIBRELANE_WORKDIR_RETENTION_DAYS", "0")
)

# Flow artifacts can be large when persisted into Postgres.
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024 * 1024


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
