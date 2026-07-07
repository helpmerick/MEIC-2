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
            if f.name not in data:
                # Field absent in an older log entry — fall back to its default
                # (schema evolution: e.g. `fee` added after early events).
                continue
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


# `fee` on every fill-bearing event: the per-contract commissions/fees (PNL-01)
# incurred by THAT fill, RECORDED AT FILL TIME from the FeeModel then in force.
# Recording (not recomputing) keeps replay deterministic (PNL-03) and lets the
# EOD pass reconcile recorded fees against broker truth (PNL-04). Default 0.00
# is the seam only — the FeeModel that populates it lands with the code that
# produces each fill (stop fills: slice 2; entry fills: slice 3).

@dataclass(frozen=True)
class CondorFilled(Event):
    entry_id: str
    net_credit: Decimal  # actual net fill credit (STK-02a) — the P&L basis
    fee: Decimal = Decimal("0")  # entry fees, all four legs (PNL-01)
    short_premium: Decimal = Decimal("0")  # gross premium on the shorts (UI-14 label)


@dataclass(frozen=True)
class StopPlaced(Event):
    entry_id: str
    side: str
    trigger: Decimal  # STP-01/02: broker-resting buy-to-close stop-market


@dataclass(frozen=True)
class StopReplaced(Event):
    entry_id: str
    side: str  # REC-04(3): stop re-placed on recovery (trigger recomputed at placement)


@dataclass(frozen=True)
class ReconciliationMismatch(Event):
    detail: str  # REC-02: broker vs internal disagreement -> RSK-03 gate


@dataclass(frozen=True)
class StopConfirmed(Event):
    entry_id: str
    side: str  # STP-04: working-order confirmation from broker


@dataclass(frozen=True)
class SideUnprotected(Event):
    entry_id: str
    side: str
    action: str  # STP-04: flatten_side | flatten_condor after retries exhausted


@dataclass(frozen=True)
class WatchdogEscalated(Event):
    entry_id: str
    side: str
    mark_at_breach: Decimal   # calibration evidence (STP-03b / TC-STP-17)
    elapsed_seconds: Decimal
    fill_price: Decimal


@dataclass(frozen=True)
class EntryClosedInfeasible(Event):
    entry_id: str  # STP-02c post-fill: closed via CLS, initiator infeasible_stop


@dataclass(frozen=True)
class ShortStopped(Event):
    entry_id: str
    side: str  # "PUT" | "CALL"
    fill: Decimal  # buy-to-close fill price paid
    slippage: Decimal
    fee: Decimal = Decimal("0")  # buy-to-close fee (PNL-01)
    initiator: str = "resting_stop"  # resting_stop | watchdog_escalation (STP-03b)


@dataclass(frozen=True)
class LongSold(Event):
    entry_id: str
    side: str
    recovery: Decimal  # credit received selling the orphaned long (LEX)
    fee: Decimal = Decimal("0")  # long-sale fee (PNL-01)


@dataclass(frozen=True)
class SideClosed(Event):
    entry_id: str
    side: str


@dataclass(frozen=True)
class SideExpired(Event):
    entry_id: str
    side: str  # cash-settled worthless (EOD-01) — no cash movement


@dataclass(frozen=True)
class EntryClosed(Event):
    entry_id: str
    initiator: str  # CLS-02/04: manual | manual_flatten | take_profit | eod | decay | infeasible_stop


@dataclass(frozen=True)
class LongSaleStarted(Event):
    entry_id: str
    side: str


@dataclass(frozen=True)
class LongSaleRepriced(Event):
    entry_id: str
    side: str
    step: int
    price: Decimal


@dataclass(frozen=True)
class ForeignDetected(Event):
    symbol: str  # OWN-03: FOREIGN quarantine, alert-only


@dataclass(frozen=True)
class ForeignReduction(Event):
    symbol: str  # OWN-06: broker shows less than ledger -> SUSPEND + write down
    from_qty: int
    to_qty: int


@dataclass(frozen=True)
class EntryCompleted(Event):
    entry_id: str
