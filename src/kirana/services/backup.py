"""Production-grade database backup, integrity verification, and disaster recovery.

Provides:
  - SQLite WAL flush & checkpointing
  - Full state snapshot archiving (.zip) including master DB, store DBs, and logos
  - Deep archive verification using PRAGMA integrity_check
  - In-place safe state restoration
  - Telegram Cloud Vault synchronization (pinned backup auto-restore on container boot)
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
import zipfile

from ..config import (
    ARTIFACTS_DIR,
    BACKUP_CHAT_ID,
    DB_PATH,
    MASTER_DB_PATH,
    PROJECT_ROOT,
    STORES_DIR,
)

logger = logging.getLogger(__name__)


def checkpoint_sqlite_db(db_path: Path) -> bool:
    """Run WAL checkpoint on a single SQLite database to commit all pending WAL transactions."""
    if not db_path.exists() or db_path.stat().st_size == 0:
        return False
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        return True
    except Exception as exc:
        logger.warning("Failed to checkpoint WAL for %s: %s", db_path, exc)
        return False


def checkpoint_all_databases() -> None:
    """Flush and truncate WAL on master DB and all tenant databases."""
    data_dir = PROJECT_ROOT / "data"
    if not data_dir.exists():
        return

    checkpoint_sqlite_db(MASTER_DB_PATH)
    checkpoint_sqlite_db(DB_PATH)

    if STORES_DIR.exists():
        for store_db in STORES_DIR.glob("*.db"):
            checkpoint_sqlite_db(store_db)


def create_backup_archive(output_path: Path | None = None) -> Path:
    """Create a compressed zip snapshot of all databases, tenant data, and logos.

    Returns the absolute path to the generated zip file.
    """
    checkpoint_all_databases()

    data_dir = PROJECT_ROOT / "data"
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = ARTIFACTS_DIR / f"kirana_backup_{timestamp}.zip"

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in data_dir.rglob("*"):
            if not file_path.is_file():
                continue
            # Skip WAL/SHM locks and temporary audio/test files
            if file_path.name.endswith(("-shm", "-wal", ".tmp", ".oga", ".lock")):
                continue
            arcname = file_path.relative_to(data_dir)
            zf.write(file_path, arcname=str(arcname))

    logger.info("Created backup archive at %s (%d bytes)", output_path, output_path.stat().st_size)
    return output_path


def verify_backup_archive(archive_path: Path) -> tuple[bool, str]:
    """Verify that a zip archive contains valid SQLite databases with intact integrity."""
    if not archive_path.exists():
        return False, "Archive file does not exist"

    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            file_names = zf.namelist()
            # Normalize paths for Windows/Linux
            normalized = [p.replace("\\", "/") for p in file_names]
            has_master = any(p == "master.db" or p.endswith("/master.db") for p in normalized)
            has_stores = any("stores/" in p and p.endswith(".db") for p in normalized)

            if not has_master and not has_stores:
                return False, "Archive does not contain master.db or store databases"

            # Extract to temporary directory and run PRAGMA integrity_check
            with tempfile.TemporaryDirectory() as tmpdir:
                zf.extractall(tmpdir)
                tmp_path = Path(tmpdir)
                for db_file in tmp_path.rglob("*.db"):
                    try:
                        conn = sqlite3.connect(str(db_file), timeout=5.0)
                        cursor = conn.cursor()
                        cursor.execute("PRAGMA integrity_check")
                        row = cursor.fetchone()
                        conn.close()
                        if not row or row[0] != "ok":
                            return False, f"Integrity check failed for {db_file.name}: {row}"
                    except Exception as exc:
                        return False, f"Failed reading database {db_file.name}: {exc}"

        return True, "Integrity check passed"
    except zipfile.BadZipFile:
        return False, "Invalid or corrupted zip archive"
    except Exception as exc:
        return False, f"Verification error: {exc}"


def restore_backup_archive(archive_path: Path) -> dict[str, Any]:
    """Restore state from a verified backup zip archive over PROJECT_ROOT/data.

    Returns summary metrics of the restored state.
    """
    valid, reason = verify_backup_archive(archive_path)
    if not valid:
        raise ValueError(f"Cannot restore invalid backup archive: {reason}")

    checkpoint_all_databases()

    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    files_restored = 0
    stores_restored = 0

    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.infolist():
            # Security: prevent zip traversal attacks
            target = (data_dir / member.filename).resolve()
            if not str(target).startswith(str(data_dir.resolve())):
                logger.warning("Skipping suspicious zip member: %s", member.filename)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())

            files_restored += 1
            if member.filename.replace("\\", "/").startswith("stores/") and member.filename.endswith(".db"):
                stores_restored += 1

    # Reload database connections and verify
    try:
        from ..db.master import get_master_registry

        registry = get_master_registry()
        tenants = registry.list_all_tenants()
        stores_restored = len(tenants)
    except Exception as exc:
        logger.warning("Could not refresh master registry after restore: %s", exc)

    logger.info(
        "Successfully restored state: %d files, %d stores", files_restored, stores_restored
    )
    return {
        "success": True,
        "files_restored": files_restored,
        "stores_restored": stores_restored,
        "archive_size": archive_path.stat().st_size,
    }


async def upload_backup_to_telegram(
    bot: Any,
    chat_id: int,
    pin: bool = True,
    caption_prefix: str = "📦 #kirana_backup",
) -> Any:
    """Package current database state and upload to Telegram Cloud Vault."""
    from ..db.master import get_master_registry

    archive_path = create_backup_archive()
    registry = get_master_registry()
    tenants = registry.list_all_tenants()

    caption = (
        f"{caption_prefix}\n"
        f"<b>Supermarket Ops Agent Cloud Vault</b>\n"
        f"• 🏪 Active Stores: <b>{len(tenants)}</b>\n"
        f"• 📅 Snapshot: <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</code>\n"
        f"• 💾 Archive Size: <code>{round(archive_path.stat().st_size / 1024, 1)} KB</code>\n\n"
        f"<i>This archive contains all tenant databases, catalog items, khata ledgers, and logos.</i>"
    )

    with open(archive_path, "rb") as fh:
        msg = await bot.send_document(
            chat_id=chat_id,
            document=fh,
            caption=caption,
            parse_mode="HTML",
        )

    if pin:
        try:
            await bot.pin_chat_message(
                chat_id=chat_id,
                message_id=msg.message_id,
                disable_notification=True,
            )
            logger.info("Pinned latest backup message %d in chat %s", msg.message_id, chat_id)
        except Exception as exc:
            logger.debug("Could not pin backup message in chat %s: %s", chat_id, exc)

    return msg


async def auto_restore_if_needed(bot: Any) -> bool:
    """Check if the container booted on an ephemeral disk with empty state.

    If so, download and restore the latest backup from the Telegram Cloud Vault.
    """
    if not BACKUP_CHAT_ID:
        return False

    # Check if local store state already exists
    has_local_stores = False
    try:
        from ..db.master import get_master_registry

        registry = get_master_registry()
        tenants = registry.list_all_tenants()
        if len(tenants) > 0:
            has_local_stores = True
    except Exception:
        has_local_stores = False

    if has_local_stores:
        logger.info("Local store database exists (%d tenants). Skipping auto-restore.", len(tenants))
        return False

    logger.info("Fresh/ephemeral container detected. Checking Telegram Cloud Vault (%s)...", BACKUP_CHAT_ID)
    try:
        chat = await bot.get_chat(BACKUP_CHAT_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            logger.info("No pinned backup document found in backup chat %s", BACKUP_CHAT_ID)
            return False

        doc = pinned.document
        if not doc.file_name or not doc.file_name.endswith(".zip"):
            logger.info("Pinned message document is not a zip backup: %s", doc.file_name)
            return False

        logger.info("Downloading latest cloud backup: %s (%d bytes)...", doc.file_name, doc.file_size)
        file_obj = await bot.get_file(doc.file_id)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_f:
            tmp_path = Path(tmp_f.name)

        await file_obj.download_to_drive(custom_path=str(tmp_path))

        valid, reason = verify_backup_archive(tmp_path)
        if not valid:
            logger.error("Downloaded cloud backup failed verification: %s", reason)
            return False

        res = restore_backup_archive(tmp_path)
        logger.info("✅ State successfully restored from Telegram Cloud Vault! %s", res)
        try:
            tmp_path.unlink()
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.warning("Failed auto-restoring from Telegram Cloud Vault: %s", exc, exc_info=True)
        return False
