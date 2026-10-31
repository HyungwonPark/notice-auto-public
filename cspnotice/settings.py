"""Django settings for the CSP notice automation (internal VM, reached only from the company network)."""
import os
import sys
from pathlib import Path

# RHEL 8's system SQLite (3.26) is older than Django 5.2 supports (3.31+); use the bundled build.
import pysqlite3

sys.modules["sqlite3"] = pysqlite3

BASE_DIR = Path(__file__).resolve().parent.parent
VAR_DIR = Path(os.environ.get("CSP_VAR_DIR", "/data/csp-notice/var"))
DEV_SECRET_KEY = "dev-only-not-secret"


def _secret_key() -> str:
    """CSP_SECRET_KEY, else /etc/csp-notice/secret_key (created by ops/systemd/install.sh)."""
    if os.environ.get("CSP_SECRET_KEY"):
        return os.environ["CSP_SECRET_KEY"]
    try:
        return Path("/etc/csp-notice/secret_key").read_text(encoding="ascii").strip()
    except OSError:
        return DEV_SECRET_KEY  # tests and local commands; cspnotice/wsgi.py refuses it for the web server


SECRET_KEY = _secret_key()
DEBUG = False
ALLOWED_HOSTS = os.environ.get("CSP_ALLOWED_HOSTS", "192.0.2.10,localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "mailcollect",
    "customers",
    "notifications",
    "suggestions",
    "web",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "web.middleware.PasswordChangeRequired",
    "web.middleware.ManagerOnly",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "cspnotice.urls"
WSGI_APPLICATION = "cspnotice.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "web.context.nav",
            ]
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": VAR_DIR / "db.sqlite3",
        "OPTIONS": {
            "timeout": 20,
            # WAL: readers and the one writer no longer block each other, so a scan, the web and a command can
            # run together (2026-10-19: rewriting drafts during a scan made two scans fail with "locked").
            # IMMEDIATE takes the write lock when a transaction starts, so a busy DB waits instead of failing
            # half-way through.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "ko-kr"
USE_TZ = True
TIME_ZONE = "Asia/Seoul"

STATIC_URL = "/static/"
STATIC_ROOT = Path(os.environ.get("CSP_STATIC_ROOT", "/data/csp-notice/static"))
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Plain HTTP on the internal network (handoff decision): cookies cannot be marked Secure, so keep
# sessions short and the cookies away from scripts and other sites.
SESSION_COOKIE_AGE = 12 * 3600
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"
X_FRAME_OPTIONS = "DENY"

# Read-only POP3 collection. Credentials live only in this env file, never in code or git.
CSP_POP3_ENV_FILE = os.environ.get("CSP_POP3_ENV", "/etc/csp-notice/pop3.env")
CSP_POP3_EXTRA_DIR = os.environ.get("CSP_POP3_EXTRA_DIR", "/etc/csp-notice/mailboxes")  # <name>.env per extra mailbox
CSP_OPSDB_ENV = os.environ.get("CSP_OPSDB_ENV", "/etc/csp-notice/opsdb.env")  # company MySQL view of customer accounts
CSP_SENS_ENV = os.environ.get("CSP_SENS_ENV", "/etc/csp-notice/sens.env")  # SENS mail sending (notifications/sens.py)
CSP_SEND_LOCK = str(VAR_DIR / "send.lock")
CSP_SCAN = {
    # Per run, newest first; the rest waits for the next run. Busiest 5 minutes seen (2026-10-19..21): 117 new
    # mails over both mailboxes; 200 TOPs took 15 s on average. max_seconds caps a run whatever these say.
    "batch_size": 500,  # TOP commands per run
    "body_batch_size": 100,  # RETR commands per run
    "timeout": 30,  # socket timeout, seconds
    "max_seconds": 60,  # stop sending TOP/RETR after this; POP3 may lock the maildrop for the whole session
    "lock_path": str(VAR_DIR / "scan.lock"),
    "request_path": str(VAR_DIR / "scan.request"),  # watched by csp-notice-scan.path
    "request_min_interval": 10,  # seconds; a scan request right after a finished scan is ignored
}
CSP_BACKUP_DIR = Path(os.environ.get("CSP_BACKUP_DIR", "/data/csp-notice/backup"))
CSP_BACKUP_KEEP_DAYS = 14
CSP_KEEP_DAYS = 365  # works, mail and history older than this are deleted nightly (purge_old)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"mailcollect": {"handlers": ["console"], "level": "INFO"}},
}
