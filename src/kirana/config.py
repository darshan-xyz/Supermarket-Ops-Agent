"""Runtime configuration, loaded from the environment (never hard-coded secrets).

`.env` is loaded if present (local dev). In production, real env vars win.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv optional at runtime
    pass


def _path(env_key: str, default: str) -> Path:
    p = Path(os.environ.get(env_key, default)).expanduser().resolve()
    return p


def _get_project_root() -> Path:
    if "KIRANA_PROJECT_ROOT" in os.environ:
        return Path(os.environ["KIRANA_PROJECT_ROOT"]).resolve()
    # If pyproject.toml exists in cwd, use cwd
    cwd = Path.cwd().resolve()
    if (cwd / "pyproject.toml").exists() or (cwd / "skills").exists():
        return cwd
    # Check parent directory hierarchy from __file__
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return cwd


# Repo root
PROJECT_ROOT: Path = _get_project_root()

# --- Storage ---
DB_PATH: Path = _path("KIRANA_DB_PATH", str(PROJECT_ROOT / "data" / "kirana.db"))
MASTER_DB_PATH: Path = _path("KIRANA_MASTER_DB_PATH", str(PROJECT_ROOT / "data" / "master.db"))
STORES_DIR: Path = _path("KIRANA_STORES_DIR", str(PROJECT_ROOT / "data" / "stores"))
ARTIFACTS_DIR: Path = _path("KIRANA_ARTIFACTS_DIR", str(PROJECT_ROOT / "artifacts_out"))

# --- Secrets / API ---
GEMINI_API_KEY: str | None = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
TELEGRAM_BOT_TOKEN: str | None = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_GATEWAY_TOKEN: str | None = os.environ.get("TELEGRAM_GATEWAY_TOKEN")

# --- Model --- (override with KIRANA_MODEL, e.g. gemini-3.1-flash-lite)
MODEL: str = os.environ.get("KIRANA_MODEL", "gemini-3.1-flash-lite")
FALLBACK_MODELS: list[str] = [
    MODEL,
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
]

# --- Agent behaviour ---
MAX_TURNS: int = int(os.environ.get("KIRANA_MAX_TURNS", "24"))

# --- Scheduled jobs (IST) ---
# Optional: restrict scheduled messages to one chat; otherwise every chat that has
# used the bot receives them.
OWNER_CHAT_ID: int | None = (
    int(os.environ["KIRANA_OWNER_CHAT_ID"]) if os.environ.get("KIRANA_OWNER_CHAT_ID") else None
)
DECK_WEEKDAY: int = int(os.environ.get("KIRANA_DECK_WEEKDAY", "1"))   # 0=Sun … 1=Mon
DECK_HOUR: int = int(os.environ.get("KIRANA_DECK_HOUR", "9"))         # weekly deck at 09:00 IST
REMINDER_HOUR: int = int(os.environ.get("KIRANA_REMINDER_HOUR", "10"))  # daily khata reminder
BACKUP_HOUR: int = int(os.environ.get("KIRANA_BACKUP_HOUR", "23"))      # nightly backup at 23:00 IST

# --- Cloud Hosting & State Resilience ---
PORT: int = int(os.environ.get("PORT", "10000"))
BACKUP_CHAT_ID: int | None = (
    int(os.environ["TELEGRAM_BACKUP_CHAT_ID"])
    if os.environ.get("TELEGRAM_BACKUP_CHAT_ID")
    else OWNER_CHAT_ID
)
AUTO_RESTORE_ON_BOOT: bool = os.environ.get("BACKUP_AUTO_RESTORE_ON_BOOT", "1").lower() in (
    "1",
    "true",
    "yes",
)


def ensure_dirs() -> None:
    """Create the data and artifact directories if missing."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    MASTER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORES_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def require_telegram_token() -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set.\n"
            "  1. Talk to @BotFather on Telegram, /newbot, copy the token.\n"
            "  2. Put TELEGRAM_BOT_TOKEN=... in your .env (see .env.example).\n"
        )
    return TELEGRAM_BOT_TOKEN
