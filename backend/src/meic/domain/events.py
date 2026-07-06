"""Domain events — the event-sourced core (REC-01, doc 05 §4).

Every aggregate mutation is one of these, appended to the log before any side
effect. Events are immutable, deterministically ordered by their stream
sequence, and round-trip through a stable dict form so the log survives
process death and replays identically (REC-01 / TC-REC-01).

Money fields are Decimal end to end; serialization keeps them exact (str),
never float — a replayed P&L must equal the original to the cent.

This module is pure (no I/O). The store that persists these lives in
adapters/persistence; the fold that projects them lives in projection.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import Decimal
from typing import Any, ClassVar


class Event:
    """Base for all domain events. Subclasses are frozen dataclasses.

    `type` is the stable wire name (class name); the registry maps it back for
    deserialization. Subclasses declare only data fields — no behavior.
    """

    type: ClassVar[str]
    _registry: ClassVar[dict[str, type["Event"]]] = {}

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        cls.type = cls.__name__
        Event._registry[cls.__name__] = cls

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        for f in fields(self):  # type: ignore[arg-type]
            v = getattr(self, f.name)
            out[f.name] = str(v) if isinstance(v, Decimal) else v
        return out

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Event":
        cls = Event._registry[data["type"]]
        kwargs: dict[str, Any] = {}
        for f in fields(cls):  # type: ignore[arg-type]
            raw = data[f.name]
            kwargs[f.name] = Decimal(raw) if f.type in ("Decimal", Decimal) else raw
        return cls(**kwargs)


# --- TradingDay (doc 05 §3) --------------------------------------------------

@dataclass(frozen=True)
class DayArmed(Event):
    date: str
    entry_count: int


@dataclass(frozen=True)
class EntryWindowOpened(Event):
    date: str
    entry_number: int


@dataclass(frozen=True)
class EntrySkipped(Event):
    date: str
    entry_number: int
    reason: str


@dataclass(frozen=True)
class DayCompleted(Event):
    date: str


# --- CondorEntry (doc 05 §3) -------------------------------------------------

@dataclass(frozen=True)
class CondorProposed(Event):
    entry_id: str
    put_short: Decimal
    call_short: Decimal


@dataclass(frozen=True)
class CondorFilled(Event):
    entry_id: str
    net_credit: Decimal  # actual net fill credit (STK-02a) — the P&L basis


@dataclass(frozen=True)
class ShortStopped(Event):
    entry_id: str
    side: str  # "PUT" | "CALL"
    fill: Decimal  # buy-to-close fill price paid
    slippage: Decimal


@dataclass(frozen=True)
class LongSold(Event):
    entry_id: str
    side: str
    recovery: Decimal  # credit received selling the orphaned long (LEX)


@dataclass(frozen=True)
class SideClosed(Event):
    entry_id: str
    side: str


@dataclass(frozen=True)
class SideExpired(Event):
    entry_id: str
    side: str  # cash-settled worthless (EOD-01) — no cash movement


@dataclass(frozen=True)
class EntryCompleted(Event):
    entry_id: str
