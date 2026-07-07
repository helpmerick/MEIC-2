"""SQLite persistence adapters — event log (REC-01) and REC-07 state store.

Durable and single-writer (doc 05 §4): each event is appended with an
fsync'd commit before any side effect runs, and the log is the source of
truth a rebuilt process folds on boot. "Crash/restart" in the doc-04 harness
is exactly: close this object, open a new one on the SAME file, replay.

I/O lives here in the adapter layer — the domain stays pure (doc 05 preamble).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from meic.domain.events import Event


class SqliteEventStore:
    """Append-only, per-stream ordered event log (EventStore port)."""

    def __init__(self, path: str | Path) -> None:
        # check_same_thread=False: the ASGI server touches the store from its
        # threadpool; SQLite serializes writes and busy_timeout waits out locks.
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")  # fsync before side effects
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            " stream TEXT NOT NULL,"
            " payload TEXT NOT NULL)"
        )

    def append(self, stream: str, events: list[Event]) -> None:
        self._conn.execute("BEGIN")
        try:
            for e in events:
                self._conn.execute(
                    "INSERT INTO events (stream, payload) VALUES (?, ?)",
                    (stream, json.dumps(e.to_dict())),
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def read(self, stream: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT payload FROM events WHERE stream = ? ORDER BY seq", (stream,)
        ).fetchall()
        return [Event.from_dict(json.loads(p)) for (p,) in rows]

    def streams(self) -> list[str]:
        rows = self._conn.execute("SELECT DISTINCT stream FROM events ORDER BY stream").fetchall()
        return [s for (s,) in rows]

    def close(self) -> None:
        self._conn.close()


class SqliteStateStore:
    """Durable KV backing the REC-07 inventory (StateStore port)."""

    def __init__(self, path: str | Path) -> None:
        # check_same_thread=False: the ASGI server reads/writes state from its
        # request threadpool (see SqliteEventStore for the rationale).
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    def get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def all(self) -> dict[str, str]:
        return {k: v for k, v in self._conn.execute("SELECT key, value FROM state").fetchall()}

    def close(self) -> None:
        self._conn.close()


class InMemoryStateStore:
    """Non-durable StateStore for pure unit tests (no crash semantics)."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def set(self, key: str, value: str) -> None:
        self._d[key] = value

    def all(self) -> dict[str, str]:
        return dict(self._d)
