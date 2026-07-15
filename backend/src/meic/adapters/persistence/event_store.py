"""SQLite persistence adapters — event log (REC-01) and REC-07 state store.

Durable and single-writer (doc 05 §4): each event is appended with an
fsync'd commit before any side effect runs, and the log is the source of
truth a rebuilt process folds on boot. "Crash/restart" in the doc-04 harness
is exactly: close this object, open a new one on the SAME file, replay.

I/O lives here in the adapter layer — the domain stays pure (doc 05 preamble).
"""
from __future__ import annotations

import inspect
import json
import sqlite3
import sys
import threading
from dataclasses import fields
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from meic.domain.events import Event, FilledLeg


class SqliteEventStore:
    """Append-only, per-stream ordered event log (EventStore port)."""

    def __init__(self, path: str | Path) -> None:
        # check_same_thread=False: the ASGI server touches the store from its
        # threadpool; SQLite serializes writes and busy_timeout waits out locks.
        #
        # 2026-07 hotfix (production race, logs/panel-2026-07-13-own01.err.log):
        # check_same_thread=False permits the SAME connection object to be
        # entered from the threadpool (sync endpoints) and the event-loop
        # thread (supervisor/watchers/attempt tasks) AT THE SAME TIME. SQLite's
        # own serialization is per-call, not per-connection-object across
        # Python threads holding no GIL-independent guard — two threads racing
        # a single sqlite3.Connection corrupts its internal statement/cursor
        # state ("bad parameter or other API misuse"), not just its data. This
        # lock makes the whole connection single-writer/single-reader at a
        # time, matching the class's own "single-writer" docstring promise
        # above. Coarse and obviously-correct on purpose (doc 05 §4 recorder,
        # not a hot path) — every method touching `self._conn` holds it.
        self._lock = threading.RLock()
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
        with self._lock:
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
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM events WHERE stream = ? ORDER BY seq", (stream,)
            ).fetchall()
        return [Event.from_dict(json.loads(p)) for (p,) in rows]

    def streams(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT stream FROM events ORDER BY stream").fetchall()
        return [s for (s,) in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SqliteStateStore:
    """Durable KV backing the REC-07 inventory (StateStore port)."""

    def __init__(self, path: str | Path) -> None:
        # Recorded so a sibling durable object (EventJournal) can open its OWN
        # connection to the SAME db file — REC-01/REC-07(8) durable event log
        # lives beside REC-07's state KV in one state.db (composition/live.py).
        self.path = Path(path)
        # check_same_thread=False: the ASGI server reads/writes state from its
        # request threadpool (see SqliteEventStore for the rationale).
        # Same production race as SqliteEventStore above -- self._lock makes
        # every method below single-caller-at-a-time on this connection.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def all(self) -> dict[str, str]:
        with self._lock:
            return {k: v for k, v in self._conn.execute("SELECT key, value FROM state").fetchall()}

    def close(self) -> None:
        with self._lock:
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


# --- EventJournal: the durable, single-stream event log (REC-01 / REC-07(8)) ---
#
# Distinct from SqliteEventStore above (which keys events by an explicit
# `stream` for per-entry/per-day replay): this is the flat, global journal
# backing `comp.events` — the plain list every service appends domain events
# to, in wall order, across the whole process lifetime. `DurableEventLog`
# (application/event_log.py) is the write-through list; this class is pure
# I/O — open a connection, append a row, read rows back.
#
# Serialization is self-describing by RUNTIME VALUE, not by the dataclass
# field's string type annotation (`from __future__ import annotations` turns
# annotations like `Decimal | None` into plain strings, which the existing
# generic `Event.to_dict`/`from_dict` matches by exact string comparison —
# fragile, and wrong for Optional[Decimal] fields such as
# `EntryMarkSample.spot`). Tagging by `isinstance` on the actual value never
# has that problem and needs no per-field type bookkeeping.

def _encode_value(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return {"__decimal__": str(v)}
    if isinstance(v, datetime):
        return {"__datetime__": v.isoformat()}
    if isinstance(v, date):
        return {"__date__": v.isoformat()}
    if isinstance(v, tuple):
        if v and isinstance(v[0], FilledLeg):
            return {"__legs__": [leg.to_dict() for leg in v]}
        return {"__tuple__": [_encode_value(x) for x in v]}
    return v


def _decode_value(v):
    if isinstance(v, dict):
        if "__decimal__" in v:
            return Decimal(v["__decimal__"])
        if "__datetime__" in v:
            return datetime.fromisoformat(v["__datetime__"])
        if "__date__" in v:
            return date.fromisoformat(v["__date__"])
        if "__legs__" in v:
            return tuple(FilledLeg.from_dict(d) for d in v["__legs__"])
        if "__tuple__" in v:
            return tuple(_decode_value(x) for x in v["__tuple__"])
    return v


def _event_registry() -> dict[str, type[Event]]:
    """The registry, built by INTROSPECTING `meic.domain.events` for Event
    subclasses actually DEFINED there (never a hand-maintained list, and
    deliberately NOT `Event._registry` — that global auto-registers ANY
    subclass anywhere, including one a test file might define for its own
    purposes, which would silently widen what the journal accepts)."""
    from meic.domain import events as events_module

    registry: dict[str, type[Event]] = {}
    for _name, obj in vars(events_module).items():
        if (inspect.isclass(obj) and issubclass(obj, Event) and obj is not Event
                and obj.__module__ == events_module.__name__):
            registry[obj.__name__] = obj
    return registry


def encode_event(event: Event) -> dict:
    data: dict = {"type": type(event).__name__}
    for f in fields(event):  # type: ignore[arg-type]
        data[f.name] = _encode_value(getattr(event, f.name))
    if event.config_version:
        data["config_version"] = event.config_version
    return data


def decode_event(data: dict, registry: dict[str, type[Event]] | None = None) -> Event | None:
    """Decode one journal row's payload. Returns None for a type not in the
    registry (forward-compat: an event type from a newer build, or a retired
    one) — the caller (EventJournal.load) is the one that warns and skips."""
    registry = registry if registry is not None else _event_registry()
    cls = registry.get(data.get("type"))
    if cls is None:
        return None
    kwargs = {}
    for f in fields(cls):  # type: ignore[arg-type]
        if f.name in data:
            kwargs[f.name] = _decode_value(data[f.name])
    event = cls(**kwargs)
    if data.get("config_version"):
        object.__setattr__(event, "config_version", data["config_version"])
    return event


class EventJournal:
    """Append-only, single-stream durable event log — REC-01 ("all state
    transitions persisted as an append-only event log BEFORE being acted
    on") + REC-07 item 8 (boot restore) + doc 10 Principle 4 (deterministic
    replay-from-genesis). Lives in the SAME state.db as SqliteStateStore, in
    its own `events` table, via its own connection (WAL tolerates multiple
    connections to one file; mirrors SqliteStateStore's pragmas exactly).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # Same production race as SqliteEventStore/SqliteStateStore above --
        # self._lock makes every method below single-caller-at-a-time on this
        # connection (the event-loop thread journals through DurableEventLog
        # while the ASGI threadpool can concurrently read via a sibling
        # StateStore/EventStore on the same db file, or another endpoint that
        # touches this journal directly).
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            " at TEXT NOT NULL,"
            " type TEXT NOT NULL,"
            " config_version TEXT NOT NULL,"
            " payload TEXT NOT NULL)"
        )

    def append(self, event: Event) -> None:
        """Serialize + insert SYNCHRONOUSLY — the append must land before any
        side effect the caller performs next (REC-01). An event whose class
        is not one `meic.domain.events` actually defines is refused loudly:
        a durable log that silently swallowed an unrecognized object could
        never replay it back."""
        registry = _event_registry()
        cls = type(event)
        if registry.get(cls.__name__) is not cls:
            raise ValueError(
                f"EventJournal.append: {cls.__name__!r} is not a domain event defined in "
                "meic.domain.events — refusing to journal an unrecognized object")
        payload = json.dumps(encode_event(event))
        at = datetime.now().astimezone().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (at, type, config_version, payload) VALUES (?, ?, ?, ?)",
                (at, cls.__name__, event.config_version, payload),
            )

    def load(self) -> list[Event]:
        """Every journaled event, in seq (append) order. An unknown `type`
        (forward/backward compat — a retired event, or one from a newer
        build this process doesn't have yet) is SKIPPED with a stderr
        warning, never raised — the rest of the log must still replay."""
        registry = _event_registry()
        with self._lock:
            rows = self._conn.execute(
                "SELECT type, payload FROM events ORDER BY seq"
            ).fetchall()
        out: list[Event] = []
        for type_name, payload in rows:
            if type_name not in registry:
                print(f"EventJournal.load: skipping unknown event type {type_name!r} "
                      "(forward-compat)", file=sys.stderr)
                continue
            event = decode_event(json.loads(payload), registry)
            if event is not None:
                out.append(event)
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()
