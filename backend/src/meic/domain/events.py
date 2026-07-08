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

    # v1.44 (operator-ratified: build now, not debt). The config version in force
    # when this event was recorded. It is NOT a dataclass field: making it one
    # would force every event's __eq__ and every constructor to carry it, and two
    # events that differ only in the config version are still the same fact. It
    # round-trips through to_dict/from_dict, so a replayed log knows which rules
    # produced each event.
    config_version: str = ""

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        cls.type = cls.__name__
        Event._registry[cls.__name__] = cls

    def stamped(self, config_version: str) -> "Event":
        """Return this event carrying `config_version`. Frozen, so set it through
        object.__setattr__ on a copy — the caller's event is never mutated."""
        import copy

        out = copy.copy(self)
        object.__setattr__(out, "config_version", config_version)
        return out

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        for f in fields(self):  # type: ignore[arg-type]
            v = getattr(self, f.name)
            if isinstance(v, Decimal):
                out[f.name] = str(v)
            elif isinstance(v, tuple) and v and isinstance(v[0], FilledLeg):
                out[f.name] = [leg.to_dict() for leg in v]   # ORD-09 legs
            elif isinstance(v, tuple):
                out[f.name] = list(v)
            else:
                out[f.name] = v
        if self.config_version:
            out["config_version"] = self.config_version
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
            if f.name == "legs":
                kwargs[f.name] = tuple(FilledLeg.from_dict(d) for d in raw)
            elif f.type in ("Decimal", Decimal):
                kwargs[f.name] = Decimal(raw)
            else:
                kwargs[f.name] = raw
        event = cls(**kwargs)
        if data.get("config_version"):
            object.__setattr__(event, "config_version", data["config_version"])
        return event


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


@dataclass(frozen=True)
class ModeSwitchStaged(Event):
    target: str       # "paper" | "live"
    effective: str    # DAY-05: "next_day" — never intraday (UC-10 audit trail)


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
class FilledLeg:
    """ORD-09 (v1.45): one leg of a fill, AS THE BROKER REPORTED IT.

    `symbol` is the broker's own instrument symbol, byte-identical to its payload.
    Every later order action on this leg — stop, LEX, decay buyback, close,
    flatten, watchdog escalation — uses THIS string. Nothing re-derives it from
    strike/expiry/right at action time: re-running symbology math on every use is
    the same drift class as the intent-translation defect. Reconstruction is only
    ever a cross-check that alerts on mismatch (see `crosscheck_leg_symbols`).

    `price` is the broker-ALLOCATED fill price for this leg — the data source for
    the STP-02d reconciliation records and the OWN fill-derived ledger. Paper
    records simulator-assigned symbols and prices in the same fields (SIM-05).
    """

    symbol: str
    right: str                    # "P" | "C"
    role: str                     # "short" | "long"
    qty: int
    # Broker-ALLOCATED price for this leg — never the net credit divided by four.
    # None means the broker reported no allocation, which is the honest paper case:
    # a simulator has none, and fabricating one would poison the exact field
    # STP-02d exists to reconcile (hence "real fills only").
    price: Decimal | None = None

    @property
    def side(self) -> str:
        return "PUT" if self.right == "P" else "CALL"

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "right": self.right, "role": self.role,
                "qty": self.qty, "price": None if self.price is None else str(self.price)}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "FilledLeg":
        raw = d.get("price")
        return FilledLeg(symbol=d["symbol"], right=d["right"], role=d["role"],
                         qty=int(d["qty"]), price=None if raw is None else Decimal(raw))


@dataclass(frozen=True)
class CondorFilled(Event):
    entry_id: str
    net_credit: Decimal  # actual net fill credit (STK-02a) — the P&L basis
    fee: Decimal = Decimal("0")  # entry fees, all four legs (PNL-01)
    short_premium: Decimal = Decimal("0")  # gross premium on the shorts (UI-14 label)
    legs: tuple[FilledLeg, ...] = ()  # ORD-09: broker-reported identity + allocations


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
