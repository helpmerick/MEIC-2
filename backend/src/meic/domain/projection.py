"""Deterministic replay projection — REC-01 / TC-REC-01.

A pure fold from an ordered event log to day state + P&L. "Deterministic"
is the whole point: replaying the same log always yields an equal DayState,
so crash recovery (rebuild from the log) and the replay invariant hold.

P&L model (Ash's outcome contract, TC-STP-01 v1.38; PNL-02): the entry
collects its net credit up front; each stopped side costs the buy-to-close
fill and gives back the long's recovery; fees (PNL-01) reduce every fill. So
    entry_pnl = net_credit − Σ(stop fills) + Σ(long recoveries) − Σ(fees)
e.g. credit 4.00, one side stopped at 3.80, zero recovery, zero fees ⇒ +0.20;
both sides stopped ⇒ −3.60 (about the premium, never more before slippage).

Fees are RECORDED on each fill event (events.py) and summed here — the
projection never recomputes them, so replay is deterministic (PNL-03) and the
figure reconciles against broker truth at EOD (PNL-04). The remaining PNL-02
term — settlement effects on an in-the-money expiring leg — is ~0 for the
stop-protected 0DTE case; it enters with EOD settlement (slice 4/5) and is
marked by the TC-PNL / TC-EOD reds until then.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from .events import (
    CondorFilled,
    CondorProposed,
    DayArmed,
    DayCompleted,
    EntryClosed,
    EntryCompleted,
    Event,
    EntrySkipped,
    FilledLeg,
    LongSold,
    ShortStopped,
    SideClosed,
    SideExpired,
)


@dataclass(frozen=True)
class EntryProjection:
    entry_id: str
    net_credit: Decimal = Decimal("0")
    short_premium: Decimal = Decimal("0")  # UI-14: gross short premium, labelled apart from net
    stop_fills: Decimal = Decimal("0")
    recoveries: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    sides_stopped: tuple[str, ...] = ()
    stop_initiators: tuple[str, ...] = ()  # resting_stop | watchdog_escalation | decay
    sides_closed: tuple[str, ...] = ()
    sides_expired: tuple[str, ...] = ()
    close_initiator: str | None = None  # CLS-04: how the entry closed
    completed: bool = False
    placed_at: str | None = None  # UI card: fill time (CondorFilled.at), ISO, null if absent
    legs: tuple[FilledLeg, ...] = ()  # ORD-09: broker-reported strikes/prices for the card

    @property
    def pnl(self) -> Decimal:
        return self.net_credit - self.stop_fills + self.recoveries - self.fees

    @property
    def status(self) -> str:
        """A single label for the entry's lifecycle stage (UI read model)."""
        if self.close_initiator == "decay" or "decay" in self.stop_initiators:
            return "DECAY_CLOSED"
        if self.close_initiator:
            return "CLOSED"
        if self.sides_stopped:
            return "LEX_RECOVERED" if self.recoveries else "STOPPED"
        if len(self.sides_expired) >= 2:
            return "EXPIRED"
        if self.net_credit:
            return "PROTECTED"
        return "PENDING"


@dataclass(frozen=True)
class DayState:
    date: str | None = None
    armed_entry_count: int = 0
    entries: dict[str, EntryProjection] = field(default_factory=dict)
    skipped: tuple[tuple[int, str], ...] = ()  # (entry_number, reason)
    completed: bool = False

    @property
    def day_pnl(self) -> Decimal:
        return sum((e.pnl for e in self.entries.values()), Decimal("0"))


def _entry(state: DayState, entry_id: str) -> EntryProjection:
    return state.entries.get(entry_id, EntryProjection(entry_id))


def _put(state: DayState, e: EntryProjection) -> dict[str, EntryProjection]:
    entries = dict(state.entries)
    entries[e.entry_id] = e
    return entries


def apply(state: DayState, event: Event) -> DayState:
    """Pure single-event transition. Unknown events pass through unchanged
    (a projection is a read model — it may ignore events it doesn't track)."""
    if isinstance(event, DayArmed):
        return replace(state, date=event.date, armed_entry_count=event.entry_count)
    if isinstance(event, EntrySkipped):
        return replace(state, skipped=state.skipped + ((event.entry_number, event.reason),))
    if isinstance(event, CondorProposed):
        return replace(state, entries=_put(state, _entry(state, event.entry_id)))
    if isinstance(event, CondorFilled):
        e = _entry(state, event.entry_id)
        return replace(state, entries=_put(state, replace(
            e, net_credit=e.net_credit + event.net_credit, fees=e.fees + event.fee,
            short_premium=e.short_premium + event.short_premium,
            placed_at=event.at, legs=event.legs)))
    if isinstance(event, ShortStopped):
        e = _entry(state, event.entry_id)
        return replace(state, entries=_put(state, replace(
            e, stop_fills=e.stop_fills + event.fill, fees=e.fees + event.fee,
            sides_stopped=e.sides_stopped + (event.side,),
            stop_initiators=e.stop_initiators + (event.initiator,))))
    if isinstance(event, LongSold):
        e = _entry(state, event.entry_id)
        return replace(state, entries=_put(state, replace(
            e, recoveries=e.recoveries + event.recovery, fees=e.fees + event.fee)))
    if isinstance(event, SideClosed):
        e = _entry(state, event.entry_id)
        return replace(state, entries=_put(state, replace(e, sides_closed=e.sides_closed + (event.side,))))
    if isinstance(event, SideExpired):
        e = _entry(state, event.entry_id)
        return replace(state, entries=_put(state, replace(e, sides_expired=e.sides_expired + (event.side,))))
    if isinstance(event, EntryClosed):
        e = _entry(state, event.entry_id)
        return replace(state, entries=_put(state, replace(e, close_initiator=event.initiator)))
    if isinstance(event, EntryCompleted):
        e = _entry(state, event.entry_id)
        return replace(state, entries=_put(state, replace(e, completed=True)))
    if isinstance(event, DayCompleted):
        return replace(state, completed=True)
    return state


def fold(events: list[Event]) -> DayState:
    """Rebuild day state from an ordered event log (REC-01). Deterministic:
    equal input lists yield equal DayState."""
    state = DayState()
    for event in events:
        state = apply(state, event)
    return state


@dataclass(frozen=True)
class DayReport:
    """EOD-05 day report: per-entry credits, stops, recoveries, fees, realized
    P&L, and every skip with its reason. Projected from the event log — so it
    is deterministic and replayable (PNL-03)."""

    date: str | None
    entries_filled: int
    stops_hit: int
    lex_recoveries: int
    decay_closes: int
    total_credit: Decimal
    total_fees: Decimal
    day_pnl: Decimal
    skips: tuple[tuple[int, str], ...]
    per_entry_pnl: dict[str, Decimal]
    total_short_premium: Decimal = Decimal("0")  # UI-14: shown apart from net credit


def day_report(events: list[Event]) -> DayReport:
    state = fold(events)
    entries = state.entries.values()
    return DayReport(
        date=state.date,
        entries_filled=sum(1 for e in entries if e.net_credit != 0),
        stops_hit=sum(1 for e in entries for i in e.stop_initiators if i != "decay"),
        lex_recoveries=sum(1 for e in entries if e.recoveries != 0),
        decay_closes=sum(1 for e in entries if e.close_initiator == "decay"),
        total_credit=sum((e.net_credit for e in entries), Decimal("0")),
        total_fees=sum((e.fees for e in entries), Decimal("0")),
        day_pnl=state.day_pnl,
        skips=state.skipped,
        per_entry_pnl={e.entry_id: e.pnl for e in entries},
        total_short_premium=sum((e.short_premium for e in entries), Decimal("0")),
    )
