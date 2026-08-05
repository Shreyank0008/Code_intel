from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "dev-codeintel-not-for-prod"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = ["django.contrib.staticfiles", "api"]
MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
ROOT_URLCONF = "server.urls"
WSGI_APPLICATION = "server.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "frontend"],
    "APP_DIRS": False,
    "OPTIONS": {},
}]

# No database is used — jobs persist to a JSON file. Keep a dummy sqlite so Django is happy.
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "frontend"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

# ---- Code Intelligence config / guardrails ----
import os
# Only paths under these roots may be ingested (path-sandbox guardrail).
CODEINTEL_ALLOWED_ROOTS = [
    str(Path.home()),
    "/workspace",
    "/tmp",
    "/private/tmp",
]
CODEINTEL_MAX_FILES = 60000       # abort scans larger than this
CODEINTEL_MAX_BYTES = 400 * 1024 * 1024  # 400 MB cap on bytes read for LOC
CODEINTEL_ALLOW_BOOT = os.environ.get("CODEINTEL_ALLOW_BOOT", "0") == "1"  # docker boot opt-in
CODEINTEL_JOBS_FILE = str(BASE_DIR / "jobs_store.json")
# Public-demo lockdown (only ever set "1" on the internet-facing deployment,
# never locally): restricts Ingest to a fixed allowlist of public git repos,
# hides/blocks Settings + LLM Agents (API-key exposure, prompt tampering,
# spend risk), and disables the free-text AI chat assistant.
CODEINTEL_DEMO_MODE = os.environ.get("CODEINTEL_DEMO_MODE", "0") == "1"
# run-any-codebase skill's own artifact extractor (deep per-file catalog).
# Resolved through api.skill_paths: repo-local `skills/` first, then the
# original ~/.claude/skills location. Safe to import here -- skill_paths pulls
# in nothing but os/pathlib, so it doesn't touch the app registry.
from api.skill_paths import skill_script as _skill_script

CODEINTEL_SKILL_EXTRACTOR = str(
    _skill_script("run-any-codebase/scripts/extract_artifacts.py")
)
