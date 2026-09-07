"""SQLite connection management.

Concurrency model:
  * WAL journal  → concurrent readers never block.
  * Thread-local connections → safe use from python-telegram-bot worker threads
    and from test threads.
  * `transaction()` wraps writes in `BEGIN IMMEDIATE` under a process-wide
    re-entrant lock. The lock serialises in-process writers (the common case);
    `BEGIN IMMEDIATE` + `busy_timeout` covers any cross-connection contention.
  * Stock is decremented with a *conditional* UPDATE (see repository), so even
    if two writers race, stock can never go negative.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCHEMA = Path(__file__).with_name("schema.sql")

# Indian Standard Time (fixed +05:30) — a kirana's "today" is a local-day concept,
# so all business timestamps are stored in IST to make daily-close correct.
IST = timezone(timedelta(hours=5, minutes=30))


def now_iso() -> str:
    """Current store-local (IST) timestamp, e.g. '2026-07-11T14:03:22+05:30'."""
    return datetime.now(IST).isoformat(timespec="seconds")


def today_iso() -> str:
    return datetime.now(IST).date().isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._all_conns: set[sqlite3.Connection] = set()
        self._conns_lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.add(conn)
        return conn

    def init_schema(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            self._conn().executescript(_SCHEMA.read_text())

    # ---- reads (no write lock; WAL allows concurrent readers) ----
    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()

    # ---- writes ----
    @contextmanager
    def transaction(self):
        """Serialised write transaction (BEGIN IMMEDIATE)."""
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._conns_lock:
            for conn in list(self._all_conns):
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()
        self._local.conn = None

