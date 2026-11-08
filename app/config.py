"""Environment-driven configuration, read once at import."""

import os


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


# --- Access -----------------------------------------------------------------
AUTH_ENABLED = _bool("AUTH_ENABLED", True)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
SESSION_DAYS = _int("SESSION_DAYS", 30)
SESSION_HTTPS_ONLY = _bool("SESSION_HTTPS_ONLY", False)

# --- Data -------------------------------------------------------------------
SEED_DEMO = _bool("SEED_DEMO", False)

# --- Scheduled work ---------------------------------------------------------
REFRESH_ENABLED = _bool("REFRESH_ENABLED", True)
REFRESH_ON_START = _bool("REFRESH_ON_START", True)
REFRESH_INTERVAL_HOURS = _int("REFRESH_INTERVAL_HOURS", 24)

BACKUP_ENABLED = _bool("BACKUP_ENABLED", True)
BACKUP_INTERVAL_HOURS = _int("BACKUP_INTERVAL_HOURS", 24)
BACKUP_RETENTION_DAYS = _int("BACKUP_RETENTION_DAYS", 30)
# Backups default to living beside the database. Point this at a separate mount so
# they survive the project folder being replaced during an update.
BACKUP_DIR = os.environ.get("BACKUP_DIR", "").strip()


class ConfigError(RuntimeError):
    pass


def validate():
    """Fail loudly at startup rather than silently serving financial data unprotected."""
    # An empty APP_PASSWORD is fine: auth.ensure_password() generates one on first
    # run and keeps it in the data folder. A password that *was* given is checked.
    if not AUTH_ENABLED or not APP_PASSWORD:
        return
    if len(APP_PASSWORD) < 8:
        raise ConfigError("APP_PASSWORD must be at least 8 characters.")

    # A pasted compose file ships with a placeholder. Refuse it, or a deployment
    # that "just worked" would be sitting behind a password that is public.
    lowered = APP_PASSWORD.lower()
    # startswith rather than "contains", so a real passphrase like
    # "exchange-rate-ledger" isn't rejected for containing "change".
    placeholder = lowered.startswith("change") or lowered in {
        "password", "passphrase", "your-long-passphrase", "your-passphrase",
        "networth", "letmein", "secret", "admin",
    }
    if placeholder:
        raise ConfigError(
            f"APP_PASSWORD is still the placeholder value ({APP_PASSWORD!r}).\n"
            "  Replace it with your own passphrase, or delete the line to have one\n"
            "  generated on first start (it is printed in the log)."
        )
