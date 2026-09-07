"""Unit tests for database checkpointing, snapshot packaging, verification, and restore."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import zipfile

from kirana.services.backup import (
    checkpoint_sqlite_db,
    create_backup_archive,
    restore_backup_archive,
    verify_backup_archive,
)


def test_checkpoint_sqlite_db(tmp_path: Path):
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO test (val) VALUES ('abc')")
    conn.commit()
    conn.close()

    assert checkpoint_sqlite_db(db_file) is True


def test_verify_invalid_and_corrupt_archives(tmp_path: Path):
    # Non-existent
    valid, err = verify_backup_archive(tmp_path / "ghost.zip")
    assert valid is False

    # Corrupt zip
    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_text("not a zip file", encoding="utf-8")
    valid, err = verify_backup_archive(bad_zip)
    assert valid is False
    assert "Invalid" in err or "corrupted" in err

    # Zip without master.db or stores
    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, "w") as zf:
        zf.writestr("notes.txt", "hello world")
    valid, err = verify_backup_archive(empty_zip)
    assert valid is False
    assert "does not contain" in err


def test_create_and_verify_real_backup(tmp_path: Path):
    out_archive = tmp_path / "backup_test.zip"
    created_path = create_backup_archive(out_archive)
    assert created_path.exists()
    assert created_path.stat().st_size > 0

    valid, msg = verify_backup_archive(created_path)
    assert valid is True
    assert msg == "Integrity check passed"


def test_restore_backup_archive(tmp_path: Path, monkeypatch):
    # Setup mock project root with dummy data
    mock_root = tmp_path / "app"
    mock_data = mock_root / "data"
    mock_stores = mock_data / "stores"
    mock_stores.mkdir(parents=True)

    # Create dummy master DB
    master_db = mock_data / "master.db"
    conn = sqlite3.connect(str(master_db))
    conn.execute("CREATE TABLE tenants (id TEXT, name TEXT)")
    conn.execute("INSERT INTO tenants VALUES ('T1', 'Test Store')")
    conn.commit()
    conn.close()

    # Create dummy store DB
    store_db = mock_stores / "store_T1.db"
    conn = sqlite3.connect(str(store_db))
    conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO products VALUES (1, 'Milk 500ml')")
    conn.commit()
    conn.close()

    monkeypatch.setattr("kirana.services.backup.PROJECT_ROOT", mock_root)
    monkeypatch.setattr("kirana.services.backup.MASTER_DB_PATH", master_db)
    monkeypatch.setattr("kirana.services.backup.DB_PATH", mock_data / "kirana.db")
    monkeypatch.setattr("kirana.services.backup.STORES_DIR", mock_stores)

    backup_file = tmp_path / "test_snapshot.zip"
    create_backup_archive(backup_file)

    # Wipe mock data directory to simulate ephemeral restart
    master_db.unlink()
    store_db.unlink()

    # Restore from backup archive
    res = restore_backup_archive(backup_file)
    assert res["success"] is True
    assert res["files_restored"] >= 2

    # Verify restored SQLite contents
    assert master_db.exists()
    conn = sqlite3.connect(str(master_db))
    rows = conn.execute("SELECT name FROM tenants").fetchall()
    conn.close()
    assert rows == [("Test Store",)]

    assert store_db.exists()
    conn = sqlite3.connect(str(store_db))
    p_rows = conn.execute("SELECT name FROM products").fetchall()
    conn.close()
    assert p_rows == [("Milk 500ml",)]
