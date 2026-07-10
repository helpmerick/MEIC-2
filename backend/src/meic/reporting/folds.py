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

from meic.domain.events import DayArmed, Event, EntrySkipped, ExternalFillImported
from meic.domain.projection import EntryProjection, fold

CONTRACT_MULTIPLIER = Decimal(100)


def entry_day(entry_id: str) -> str:
    """The ET trading-day date (YYYY-MM-DD) encoded in an entry id, `"{day}#{n}"`."""
    return entry_id.split("#", 1)[0]


def trading_days(events: list[Event]) -> tuple[str, ...]:
    """RPT-01: the sorted set of qualifying trading days. RPT-16: a day
    imported from broker history (ExternalFillImported) counts as a trading
    day too, even though it was never armed/attempted by this process."""
    days: set[str] = set()
    for event in events:
        if isinstance(event, DayArmed):
            days.add(event.date)
        elif isinstance(event, EntrySkipped):
            days.add(event.date)
        elif isinstance(event, ExternalFillImported):
            days.add(event.day)
    for entry_id in fold(events).entries:
        days.add(entry_day(entry_id))
    return tuple(sorted(days))


def imported_fills_by_day(events: list[Event]) -> dict[str, tuple[ExternalFillImported, ...]]:
    """RPT-16: every imported fill leg, grouped by its own `day` field."""
    out: dict[str, list[ExternalFillImported]] = {}
    for e in events:
        if isinstance(e, ExternalFillImported):
            out.setdefault(e.day, []).append(e)
    return {day: tuple(fs) for day, fs in out.items()}


def imported_fill_dollars(fill: ExternalFillImported) -> Decimal:
    """RPT-16: real-dollar signed cash effect of one imported row.

    A settlement row (`value` present -- operator ruling 2026-07-10, RPT-16
    settlement import) carries the broker's OWN net cash effect
    (`Transaction.net_value`) directly: it is real dollars already, not a
    per-contract price, so there is no `* CONTRACT_MULTIPLIER` and no sign
    inference -- the broker already signed it. It is also already net of
    THAT row's own fee (see `imported_day_net`, which must not subtract it
    a second time).

    A Trade-style fill leg (`value` is None) keeps the original math: a
    Sell* action is a credit (+), a Buy* action is a debit (-), scaled by
    CONTRACT_MULTIPLIER (100). A fill with no broker-allocated price
    contributes 0 (honest, never fabricated)."""
    if fill.value is not None:
        return fill.value
    if fill.price is None:
        return Decimal("0")
    sign = Decimal(1) if fill.action.lower().startswith("sell") else Decimal(-1)
    return sign * fill.price * Decimal(fill.quantity) * CONTRACT_MULTIPLIER


def imported_day_fees(fills: tuple[ExternalFillImported, ...]) -> Decimal:
    """RPT-16: every imported row's own fee, settlement rows included --
    this is a TOTAL COST figure for display (drill-down `imported_cash.fees`,
    CoreResults.imported_fees), independent of whether `imported_day_net`
    below actually subtracts that particular row's fee again."""
    return sum((f.fee for f in fills if f.fee is not None), Decimal("0"))


def imported_day_net(fills: tuple[ExternalFillImported, ...]) -> Decimal:
    """RPT-16: real-dollar NET cash for one imported day.

    A settlement row's `imported_fill_dollars` is ALREADY net of its own fee
    (it is the broker's `net_value` straight through) -- subtracting
    `imported_day_fees` again for that row would double-count it. Only a
    Trade-style row's (price-based, gross) dollar figure still needs its fee
    backed out here, exactly as before this rule existed. `imported_day_fees`
    (used elsewhere for the display total, e.g. `gross_pnl = net_pnl + fees`
    in core_results) still reports every row's fee, settlement included --
    only THIS function is selective about which fee it actually subtracts."""
    gross = sum((imported_fill_dollars(f) for f in fills), Decimal("0"))
    trade_style_fees = sum(
        (f.fee for f in fills if f.value is None and f.fee is not None), Decimal("0"))
    return gross - trade_style_fees


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
    see module docstring). EOD-01 v1.59: `entry.settlements` is ALREADY real
    dollars (the broker's own signed net cash effect for a captured
    settlement) — added directly, never re-scaled by the contract
    multiplier, exactly as `imported_fill_dollars` treats a settlement row's
    `value`."""
    return entry.pnl * CONTRACT_MULTIPLIER * contracts_of(entry) + entry.settlements


def entry_credit_dollars(entry: EntryProjection) -> Decimal:
    """Real-dollar total net credit collected for one entry."""
    return entry.net_credit * CONTRACT_MULTIPLIER * contracts_of(entry)


def daily_net(events: list[Event]) -> dict[str, Decimal]:
    """Real-dollar net P&L per trading day (RPT-02/04 basis). Every qualifying
    trading day appears, even one with zero fills (0.00) — a day is never
    silently absent from the map just because nothing filled.

    RPT-16: an imported day's broker-truth cash net is added in too — the
    headline per-day number must be honest broker cash, not a fabricated
    0.00 for a day that plainly moved money. Callers that build a metrics
    INPUT series (Sharpe/MDD/etc, doc 10 rule 3) must exclude imported days
    themselves (see reports.py's `_summary_metrics`) -- this function's
    output is deliberately still complete, not pre-filtered, since the CSV
    daily-table export and RPT-01 day_win_rate both want the honest figure."""
    out = {day: Decimal("0") for day in trading_days(events)}
    for day, entries in entries_by_day(events).items():
        out[day] = sum((entry_dollars(e) for e in entries), Decimal("0"))
    for day, fills in imported_fills_by_day(events).items():
        out[day] = out.get(day, Decimal("0")) + imported_day_net(fills)
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
    premium_capture: Decimal | None  # net_pnl / total_credit, exact fraction (entries only,
                                      # NEVER diluted by imported cash -- see core_results)
    # RPT-16: broker-imported days' contribution, ADDITIVE on top of the
    # entry-based figures above (already folded into net_pnl/gross_pnl/fees).
    # Never mixed into `filled`/`fired` (entry-attempt counts) or the win-rate/
    # premium_capture ratios -- an imported leg fill is not an "entry attempt".
    imported_days: int = 0
    imported_fills: int = 0
    imported_net: Decimal = Decimal("0")
    imported_fees: Decimal = Decimal("0")


def _skip_reasons(events: list[Event]) -> dict[str, int]:
    out: dict[str, int] = {}
    for event in events:
        if isinstance(event, EntrySkipped):
            out[event.reason] = out.get(event.reason, 0) + 1
    return out


def core_results(events: list[Event]) -> CoreResults:
    """RPT-02. All money fields are REAL DOLLARS (see module docstring); win
    rates and premium capture are exact Decimal fractions — round to 2dp
    ONLY at the presentation edge (RPT-04's rounding rule applies here too).

    RPT-16: broker-imported days contribute their cash-level net and fees to
    the headline `net_pnl`/`gross_pnl`/`fees` totals (broker truth is broker
    truth), and their own leg/day counts to `imported_fills`/`imported_days`
    -- but NEVER to `filled`/`fired` (entry-attempt counts), `total_credit`,
    `entry_win_rate`, or `premium_capture`: those stay computed purely from
    real fold entries, exactly as before this rule existed, because an
    imported fill carries no recorded entry-level intent to count as one
    (REC-02) and `entries_by_day`/`fold` never produce a pseudo-entry for it."""
    by_day = entries_by_day(events)
    all_entries = [e for es in by_day.values() for e in es]
    filled_entries = [e for e in all_entries if e.net_credit != 0]

    entry_fees = sum((entry_dollars_fees(e) for e in filled_entries), Decimal("0"))
    entry_net_pnl = sum((entry_dollars(e) for e in filled_entries), Decimal("0"))
    total_credit = sum((entry_credit_dollars(e) for e in filled_entries), Decimal("0"))

    imported_by_day = imported_fills_by_day(events)
    imported_net = sum((imported_day_net(fs) for fs in imported_by_day.values()), Decimal("0"))
    imported_fees_total = sum((imported_day_fees(fs) for fs in imported_by_day.values()), Decimal("0"))
    imported_fill_count = sum(len(fs) for fs in imported_by_day.values())

    net_pnl = entry_net_pnl + imported_net
    fees = entry_fees + imported_fees_total
    gross_pnl = net_pnl + fees

    skipped_by_reason = _skip_reasons(events)
    fired = len(filled_entries) + sum(skipped_by_reason.values())

    days = trading_days(events)
    daily = daily_net(events)  # RPT-16: already includes imported-day cash (folds.daily_net)
    day_win_rate = (Decimal(sum(1 for d in days if daily[d] > 0)) / len(days)) if days else None
    entry_win_rate = (Decimal(sum(1 for e in filled_entries if e.pnl > 0)) / len(filled_entries)
                       ) if filled_entries else None
    premium_capture = (entry_net_pnl / total_credit) if total_credit else None

    return CoreResults(
        net_pnl=net_pnl, gross_pnl=gross_pnl, fees=fees,
        filled=len(filled_entries), fired=fired, skipped_by_reason=skipped_by_reason,
        total_credit=total_credit, day_win_rate=day_win_rate,
        entry_win_rate=entry_win_rate, premium_capture=premium_capture,
        imported_days=len(imported_by_day), imported_fills=imported_fill_count,
        imported_net=imported_net, imported_fees=imported_fees_total)


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
            or len(entry.sides_expired) >= 2 or len(entry.sides_stopped) >= 2
            # EOD-01 v1.59: a filled entry with nothing left settlement_pending
            # (every unstopped short's symbol has a captured SettlementRecorded)
            # is settled too -- the live path's own held-to-expiry case, which
            # emits no SideExpired at all.
            or (bool(entry.legs) and not entry.settlement_pending))


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
    back out the fee in `core_results`, rather than mixing two scales).
    EOD-01 v1.59: `entry.settlement_fees` is already real dollars (a captured
    settlement row's own fee) -- added directly, same convention as
    `entry_dollars`/`imported_day_fees`."""
    return entry.fees * CONTRACT_MULTIPLIER * contracts_of(entry) + entry.settlement_fees


def entry_trading_fees_dollars(entry: EntryProjection) -> Decimal:
    """Real-dollar ENTRY-side fees ONLY -- excludes any captured settlement's
    own fee. EOD-01 v1.59: reporting/waterfall.py needs this exclusive figure
    for its `fees` bar, because a settlement's `value` (its own `settlements`
    bar there) is already net of that same fee -- folding it into `fees` too
    would double-count it and break the waterfall's cent-exact reconciliation
    against `expected_net` (`entry_dollars`, which also already includes the
    settlement). Use `entry_dollars_fees` above for any DISPLAY total."""
    return entry.fees * CONTRACT_MULTIPLIER * contracts_of(entry)
