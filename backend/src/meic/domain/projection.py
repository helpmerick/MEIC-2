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
    Event,
    EntrySkipped,
    FilledLeg,
    LongSold,
    SettlementRecorded,
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
    placed_at: str | None = None  # UI card: fill time (CondorFilled.at), ISO, null if absent
    legs: tuple[FilledLeg, ...] = ()  # ORD-09: broker-reported strikes/prices for the card
    # EOD-01 v1.59: SUM of attributed `SettlementRecorded.value` -- REAL DOLLARS
    # already (the broker's own signed net cash effect), NOT per-share like
    # net_credit/stop_fills/recoveries/fees above. Added at the real-dollar
    # layer (reporting/folds.py `entry_dollars`), never inside `.pnl` below --
    # mixing scales there would silently double- or under-scale it.
    settlements: Decimal = Decimal("0")
    # Same real-dollar scale as `settlements` -- a display-total-only figure
    # (already netted INTO `settlements`; mirrors imported_day_fees vs
    # imported_day_net in reporting/folds.py).
    settlement_fees: Decimal = Decimal("0")
    # Every symbol with a captured SettlementRecorded -- drives
    # `settlement_pending` below.
    settled_symbols: frozenset[str] = frozenset()
    # ENT-09b v1.57: the manual-fire minimum short-strike floors this entry was
    # fired under, if any (from CondorFilled -- see events.py). None/None for
    # every scheduled or pre-v1.57 entry.
    put_floor: Decimal | None = None
    call_floor: Decimal | None = None
    # CAL-06 (v1.71): True iff this entry fired via an acknowledged manual
    # override of a NO-TRADE tag (CondorFilled.blackout_overridden) -- "the
    # entry is report-tagged blackout_overridden". False for every scheduled
    # entry and every pre-v1.71 fill.
    blackout_overridden: bool = False

    @property
    def pnl(self) -> Decimal:
        return self.net_credit - self.stop_fills + self.recoveries - self.fees

    @property
    def settlement_pending(self) -> bool:
        """EOD-01 v1.59: True while a short leg that was never stopped has
        reached the log with no `SettlementRecorded` captured yet for its
        symbol -- this entry's P&L is PROVISIONAL until the broker's own
        settlement cash lands (never guessed, never computed). An entry
        with no recorded legs (never filled), or one closed some other way
        (CLS-01, any initiator), has nothing left pending."""
        if not self.legs or self.close_initiator is not None:
            return False
        unresolved_shorts = [leg for leg in self.legs
                             if leg.role == "short" and leg.side not in self.sides_stopped]
        if not unresolved_shorts:
            return False
        return any(leg.symbol not in self.settled_symbols for leg in unresolved_shorts)

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
            placed_at=event.at, legs=event.legs,
            put_floor=event.put_floor, call_floor=event.call_floor,
            blackout_overridden=event.blackout_overridden)))
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
    if isinstance(event, SettlementRecorded):
        e = _entry(state, event.entry_id)
        return replace(state, entries=_put(state, replace(
            e, settlements=e.settlements + event.value,
            settlement_fees=e.settlement_fees + (event.fee or Decimal("0")),
            settled_symbols=e.settled_symbols | {event.symbol})))
    if isinstance(event, EntryClosed):
        e = _entry(state, event.entry_id)
        return replace(state, entries=_put(state, replace(e, close_initiator=event.initiator)))
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


def _entry_day(entry_id: str) -> str:
    """The day-bucket prefix of an entry id, `"{day}#{n}"` -- the SAME
    convention as `reporting/folds.py`'s `entry_day`, mirrored here rather than
    imported: the domain layer must never depend on the reporting layer above
    it, exactly the reason `reporting/periods.py` already keeps its own private
    `_entry_day` as a peer of `folds.py` instead of importing it."""
    return entry_id.split("#", 1)[0]


def day_report(events: list[Event], day: str | None = None) -> DayReport:
    """EOD-05 day report, SCOPED to one trading day (2026-07-13 fix — this used
    to fold the entire log with no day filter at all, so a prior day's entry
    that never reached a terminal state would count toward every later day's
    totals forever). ENT-05's max_entries_per_day gate this originally also
    guarded against is RETIRED (v1.81); the day-scoping fix stands on its own
    for every other day-scoped total this function computes.

    `day` explicit -> scope to that day. `day=None` -> default to the fold's
    own `state.date` (the date the log's most recent `DayArmed` stamped), so a
    caller that doesn't pass one still gets "today" rather than the whole log.
    If there is no date to scope by either way (no `DayArmed` at all in this
    log -- e.g. a synthetic fixture whose entry ids carry no day prefix),
    every entry is reported unscoped: there is no day concept to filter by,
    so this matches the only behaviour such a log ever had.
    """
    state = fold(events)
    scope_day = day if day is not None else state.date
    if scope_day is None:
        entries = list(state.entries.values())
        skips = state.skipped
    else:
        entries = [e for e in state.entries.values() if _entry_day(e.entry_id) == scope_day]
        skips = tuple((ev.entry_number, ev.reason) for ev in events
                       if isinstance(ev, EntrySkipped) and ev.date == scope_day)
    return DayReport(
        date=scope_day,
        entries_filled=sum(1 for e in entries if e.net_credit != 0),
        stops_hit=sum(1 for e in entries for i in e.stop_initiators if i != "decay"),
        lex_recoveries=sum(1 for e in entries if e.recoveries != 0),
        decay_closes=sum(1 for e in entries if e.close_initiator == "decay"),
        total_credit=sum((e.net_credit for e in entries), Decimal("0")),
        total_fees=sum((e.fees for e in entries), Decimal("0")),
        day_pnl=sum((e.pnl for e in entries), Decimal("0")),
        skips=skips,
        per_entry_pnl={e.entry_id: e.pnl for e in entries},
        total_short_premium=sum((e.short_premium for e in entries), Decimal("0")),
    )
