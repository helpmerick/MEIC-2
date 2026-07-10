"""RPT-01/02 period folds — pure event-log -> per-day / per-entry aggregates.

A reporting "trading day" (RPT-01) is any ET calendar day with >= 1 entry
ATTEMPT: fired (an entry whose id `"{day}#{n}"` appears in the fold — the
`{day}` prefix IS the ET date every entry attempt is scheduled/executed
under, see application/run_trading_day.py and manual_entry.py), skipped
(`EntrySkipped.date` — scheduled or ENT-09 manual, both emit it), or an armed
day bracket (`DayArmed.date`, emitted even for a day that fires nothing).
Disarmed flat days never emit ANY of the above (the day supervisor's
`_supervise_once` never starts the day task while disarmed), so they are
excluded from the day set BY CONSTRUCTION — never diluting an average
(TC-RPT-01 scenario 2).

RPT-01 also counts "an open bot position" toward a trading day even absent a
fresh attempt that day. SPX 0DTE cash-settles same-day (EOD-01) — a bot
position can never span a calendar-day boundary for this instrument — so
the attempt-based day set above is already complete; there is no multi-day
open-position case to fold in for this instrument.

`EntryProjection.net_credit` / `.pnl` are PER-SHARE amounts (matching the
existing convention in server.py's `_live_pnl_enricher`: real dollars need
`* 100 * contracts`). This module is the ONE place reporting performs that
conversion — every other reporting module receives already-dollarized
Decimals.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from meic.domain.events import DayArmed, Event, EntrySkipped
from meic.domain.projection import EntryProjection, fold

CONTRACT_MULTIPLIER = Decimal(100)


def entry_day(entry_id: str) -> str:
    """The ET trading-day date (YYYY-MM-DD) encoded in an entry id, `"{day}#{n}"`."""
    return entry_id.split("#", 1)[0]


def trading_days(events: list[Event]) -> tuple[str, ...]:
    """RPT-01: the sorted set of qualifying trading days."""
    days: set[str] = set()
    for event in events:
        if isinstance(event, DayArmed):
            days.add(event.date)
        elif isinstance(event, EntrySkipped):
            days.add(event.date)
    for entry_id in fold(events).entries:
        days.add(entry_day(entry_id))
    return tuple(sorted(days))


def entries_by_day(events: list[Event]) -> dict[str, tuple[EntryProjection, ...]]:
    """Every folded entry, grouped by the ET trading day encoded in its id."""
    out: dict[str, list[EntryProjection]] = {}
    for entry_id, entry in fold(events).entries.items():
        out.setdefault(entry_day(entry_id), []).append(entry)
    return {day: tuple(es) for day, es in out.items()}


def contracts_of(entry: EntryProjection) -> int:
    """ENT-04: each entry carries its own contracts count. `EntryProjection`
    has no dedicated field for it (slice 1 scope), so it is read off any
    recorded leg's filled quantity (ORD-01: all four legs fill balanced, so
    any one leg's qty is the entry's contracts). An entry with no recorded
    legs (never filled) contributes nothing to dollar totals regardless, so
    the fallback of 1 here is never load-bearing."""
    return entry.legs[0].qty if entry.legs else 1


def entry_dollars(entry: EntryProjection) -> Decimal:
    """Real-dollar realized P&L for one entry (contract-multiplier applied —
    see module docstring)."""
    return entry.pnl * CONTRACT_MULTIPLIER * contracts_of(entry)


def entry_credit_dollars(entry: EntryProjection) -> Decimal:
    """Real-dollar total net credit collected for one entry."""
    return entry.net_credit * CONTRACT_MULTIPLIER * contracts_of(entry)


def daily_net(events: list[Event]) -> dict[str, Decimal]:
    """Real-dollar net P&L per trading day (RPT-02/04 basis). Every qualifying
    trading day appears, even one with zero fills (0.00) — a day is never
    silently absent from the map just because nothing filled."""
    out = {day: Decimal("0") for day in trading_days(events)}
    for day, entries in entries_by_day(events).items():
        out[day] = sum((entry_dollars(e) for e in entries), Decimal("0"))
    return out


@dataclass(frozen=True)
class CoreResults:
    """RPT-02: core results for one period (already period-filtered by the
    caller — this module does not itself apply a date-range filter; that is
    a later slice's concern once the period picker exists)."""

    net_pnl: Decimal
    gross_pnl: Decimal            # net + fees (fees added back — pre-fee P&L)
    fees: Decimal
    filled: int                   # entries that reached a fill
    fired: int                    # every entry ATTEMPT (filled + skipped)
    skipped_by_reason: dict[str, int]
    total_credit: Decimal         # total net credit collected, real dollars
    day_win_rate: Decimal | None  # exact fraction of trading days with net > 0
    entry_win_rate: Decimal | None  # exact fraction of filled entries with pnl > 0
    premium_capture: Decimal | None  # net_pnl / total_credit, exact fraction


def _skip_reasons(events: list[Event]) -> dict[str, int]:
    out: dict[str, int] = {}
    for event in events:
        if isinstance(event, EntrySkipped):
            out[event.reason] = out.get(event.reason, 0) + 1
    return out


def core_results(events: list[Event]) -> CoreResults:
    """RPT-02. All money fields are REAL DOLLARS (see module docstring); win
    rates and premium capture are exact Decimal fractions — round to 2dp
    ONLY at the presentation edge (RPT-04's rounding rule applies here too)."""
    by_day = entries_by_day(events)
    all_entries = [e for es in by_day.values() for e in es]
    filled_entries = [e for e in all_entries if e.net_credit != 0]

    fees = sum((entry_dollars_fees(e) for e in filled_entries), Decimal("0"))
    net_pnl = sum((entry_dollars(e) for e in filled_entries), Decimal("0"))
    gross_pnl = net_pnl + fees
    total_credit = sum((entry_credit_dollars(e) for e in filled_entries), Decimal("0"))

    skipped_by_reason = _skip_reasons(events)
    fired = len(filled_entries) + sum(skipped_by_reason.values())

    days = trading_days(events)
    daily = daily_net(events)
    day_win_rate = (Decimal(sum(1 for d in days if daily[d] > 0)) / len(days)) if days else None
    entry_win_rate = (Decimal(sum(1 for e in filled_entries if e.pnl > 0)) / len(filled_entries)
                       ) if filled_entries else None
    premium_capture = (net_pnl / total_credit) if total_credit else None

    return CoreResults(
        net_pnl=net_pnl, gross_pnl=gross_pnl, fees=fees,
        filled=len(filled_entries), fired=fired, skipped_by_reason=skipped_by_reason,
        total_credit=total_credit, day_win_rate=day_win_rate,
        entry_win_rate=entry_win_rate, premium_capture=premium_capture)


@dataclass(frozen=True)
class DaySnapshot:
    """RPT-15's bot-computed numbers for ONE day, shaped for the broker
    reconciliation comparison (application/report_reconciler.py). `flat` is
    True only when every entry attempted that day reached a terminal state
    (closed/expired/both-sides-stopped) -- the bot's own belief about whether
    it is left holding anything after EOD-01 settlement."""

    flat: bool
    fees: Decimal
    net: Decimal
    fill_count: int


def _settled(entry: EntryProjection) -> bool:
    return (entry.completed or entry.close_initiator is not None
            or len(entry.sides_expired) >= 2 or len(entry.sides_stopped) >= 2)


def day_snapshot(events: list[Event], day: str) -> DaySnapshot:
    """RPT-15: the bot's own numbers for `day`, real dollars, ready to compare
    against a broker read-only fetch."""
    entries = entries_by_day(events).get(day, ())
    fees = sum((entry_dollars_fees(e) for e in entries), Decimal("0"))
    net = sum((entry_dollars(e) for e in entries), Decimal("0"))
    fill_count = sum(1 for e in entries if e.net_credit != 0)
    flat = all(_settled(e) for e in entries) if entries else True
    return DaySnapshot(flat=flat, fees=fees, net=net, fill_count=fill_count)


def entry_dollars_fees(entry: EntryProjection) -> Decimal:
    """Real-dollar fees for one entry. `EntryProjection.pnl` (domain/projection.py)
    computes `net_credit - stop_fills + recoveries - fees` with NO differential
    scaling between the terms — so `fees` lives at the SAME per-share scale as
    `net_credit`/`stop_fills`/`recoveries` there, and converting it to real
    dollars takes the identical contract multiplier used everywhere else in
    this module (this is what makes `gross_pnl = net_pnl + fees` correctly
    back out the fee in `core_results`, rather than mixing two scales)."""
    return entry.fees * CONTRACT_MULTIPLIER * contracts_of(entry)
