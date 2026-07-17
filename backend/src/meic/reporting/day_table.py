"""RPT-17/UI-33 (v1.82) -- the Trading tab's day-trades table and the Timing
& Unmanaged report, plus D8b's Unmanaged-P&L counterfactual.

RPT-09a (the ONE aggregation path): every money figure this module surfaces
is read straight off reporting/folds.py's canonical functions
(`entry_dollars`/`entry_credit_dollars`/`entry_dollars_fees`/`contracts_of`)
-- this module ASSEMBLES and LABELS, recomputing no P&L/credit/fee
arithmetic of its own. The genuinely NEW computations below are information
those functions do not produce at all: per-side strikes/wing-width (parsed
from the RECORDED FilledLeg symbols), per-side status badges, stop-fill
counts, ORD-11 open/close ET instants (read off the raw event log, since
`EntryProjection` does not retain them), the day's RECORDED SPX reference
(EntryMarkSample.spot only, never a retrospective fetch), and the Unmanaged
P&L counterfactual fed by the D8b sampler extension.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from meic.application.market_calendar import ET, RTH_CLOSE
from meic.domain.events import (
    CondorFilled,
    EntryClosed,
    EntryMarkSample,
    Event,
    LongSold,
    ShortStopped,
    SideClosed,
    SideExpired,
)
from meic.domain.projection import EntryProjection
from meic.reporting.folds import CONTRACT_MULTIPLIER, contracts_of, entry_credit_dollars

_SIDES = ("PUT", "CALL")
# ORD-11: every event type whose own `at` can mark when an entry's lifecycle
# moved toward closed -- the set `entry_close_at` below scans for.
_CLOSING_EVENTS = (ShortStopped, LongSold, SideClosed, SideExpired, EntryClosed)


def strike_from_symbol(symbol: str) -> Decimal:
    """OCC decode (mirrors adapters/api/app.py's `_strike_from_symbol`
    exactly: the OCC symbol's last 8 chars are the strike x1000). Reporting
    must not import the adapters/api layer (layering) -- the same reason
    domain/events.py keeps its own private `_entry_day` instead of importing
    reporting/folds.py's `entry_day`."""
    return Decimal(symbol[-8:]) / 1000


def _parse_at(at: str | None) -> datetime | None:
    """A journaled ORD-11 `at` string, parsed back to a tz-aware instant, or
    None for an absent/unparsable one -- never raises, never guessed."""
    if at is None:
        return None
    try:
        return datetime.fromisoformat(at)
    except ValueError:
        return None


def _legs_by_side(entry: EntryProjection) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {"PUT": {}, "CALL": {}}
    for leg in entry.legs:
        out.setdefault(leg.side, {})[leg.role] = leg
    return out


def wing_widths(entry: EntryProjection) -> dict[str, str | None]:
    """abs(short strike - long strike) per side, parsed straight from the
    RECORDED FilledLeg symbols (ORD-09) -- never a config lookup, so this is
    honestly what actually filled, not merely what was intended. None for a
    side missing either leg (never filled, or an honest gap)."""
    by_side = _legs_by_side(entry)
    out: dict[str, str | None] = {}
    for side in _SIDES:
        short, long_ = by_side[side].get("short"), by_side[side].get("long")
        if short is None or long_ is None:
            out[side] = None
            continue
        out[side] = str(abs(strike_from_symbol(short.symbol) - strike_from_symbol(long_.symbol)))
    return out


def side_strikes(entry: EntryProjection) -> dict[str, dict[str, str | None]]:
    """Per-side {short, long} strike, parsed from the recorded legs -- None
    for a leg that was never filled."""
    by_side = _legs_by_side(entry)
    out: dict[str, dict[str, str | None]] = {}
    for side in _SIDES:
        short, long_ = by_side[side].get("short"), by_side[side].get("long")
        out[side] = {
            "short": str(strike_from_symbol(short.symbol)) if short is not None else None,
            "long": str(strike_from_symbol(long_.symbol)) if long_ is not None else None,
        }
    return out


def side_badge(entry: EntryProjection, side: str) -> str:
    """RPT-17 per-side status badge: protected | stopped | decay | closed |
    expired | open. `EntryProjection.status` (domain/projection.py) is a
    single WHOLE-ENTRY label; RPT-17 wants one badge PER SIDE, so this
    re-derives it from the SAME per-side fields `.status` itself reads
    (sides_stopped/stop_initiators/sides_expired/sides_closed/
    close_initiator) -- never a second notion of what "stopped"/"closed"
    means, just applied side-by-side instead of entry-wide."""
    stop_initiator = dict(zip(entry.sides_stopped, entry.stop_initiators)).get(side)
    if side in entry.sides_stopped:
        return "decay" if stop_initiator == "decay" else "stopped"
    if side in entry.sides_expired:
        return "expired"
    if side in entry.sides_closed:
        return "closed"
    if entry.close_initiator is not None:
        # The whole entry closed (manual/manual_flatten/take_profit/eod/
        # infeasible_stop/decay) via an event that carries no per-side detail
        # -- every side not otherwise accounted for above closed WITH it.
        return "decay" if entry.close_initiator == "decay" else "closed"
    if not entry.legs:
        return "open"
    return "protected"


def stop_fill_count(entry: EntryProjection) -> int:
    return len(entry.sides_stopped)


def condor_filled_by_id(events: list[Event]) -> dict[str, CondorFilled]:
    """One `CondorFilled` per entry_id (emitted exactly once per successful
    fill) -- the source for `initiator`/`target_premium`, neither of which
    `EntryProjection` retains (RPT-17 is the first read model to need them)."""
    return {e.entry_id: e for e in events if isinstance(e, CondorFilled)}


def entry_close_at(entry_id: str, events: list[Event]) -> str | None:
    """ORD-11: the latest `at` among this entry's own closing-type events
    (ShortStopped/LongSold/SideClosed/SideExpired/EntryClosed) -- None while
    still open, or when none of those events carries an `at` yet (a
    pre-ORD-11 replayed log). Read off the RAW log: none of these instants
    survive onto `EntryProjection`. Returns the ORIGINAL string verbatim
    (never reformatted), same convention as reports.py's `_timeline`."""
    best_dt: datetime | None = None
    best_raw: str | None = None
    for e in events:
        if getattr(e, "entry_id", None) != entry_id or not isinstance(e, _CLOSING_EVENTS):
            continue
        dt = _parse_at(getattr(e, "at", None))
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt, best_raw = dt, e.at
    return best_raw


@dataclass(frozen=True)
class RecordedSpx:
    value: str | None
    label: str | None  # "close" | "latest" | None (no recorded spot at all)


def recorded_spx(entry_id: str, events: list[Event]) -> RecordedSpx:
    """RPT-17: "close once settled, latest recorded spot before" -- both
    values come ONLY from journaled `EntryMarkSample.spot` for this entry
    (D8b keeps sampling until the 16:00 ET close even after the entry
    closes), never a retrospective fetch. `label` is "close" once the
    LATEST recorded spot sample landed at/after the 16:00 ET close, else
    "latest". `(None, None)` when no sample ever carried a spot."""
    best_dt: datetime | None = None
    best_spot: Decimal | None = None
    for e in events:
        if not isinstance(e, EntryMarkSample) or e.entry_id != entry_id or e.spot is None:
            continue
        dt = _parse_at(e.at)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt, best_spot = dt, e.spot
    if best_spot is None:
        return RecordedSpx(value=None, label=None)
    is_close = best_dt is not None and best_dt.astimezone(ET).time() >= RTH_CLOSE
    return RecordedSpx(value=str(best_spot), label="close" if is_close else "latest")


@dataclass(frozen=True)
class UnmanagedResult:
    value: str | None
    status: str  # "ok" | "no_data"


def unmanaged_pnl(entry: EntryProjection, events: list[Event]) -> UnmanagedResult:
    """RPT-17 item 2 / D8b: premium received minus the entry's spread value
    at the 16:00 ET close, computed ONLY from recorded `EntryMarkSample`
    mids for THIS entry -- i.e. what doing nothing would have made. Uses the
    LATEST recorded sample that reached (or passed) the 16:00 ET close;
    "no_data" (D10: never interpolated) when no such sample exists, or when
    even one of its four leg mids is absent."""
    close_dt: datetime | None = None
    close_sample: EntryMarkSample | None = None
    for e in events:
        if not isinstance(e, EntryMarkSample) or e.entry_id != entry.entry_id:
            continue
        dt = _parse_at(e.at)
        if dt is None or dt.astimezone(ET).time() < RTH_CLOSE:
            continue
        if close_dt is None or dt > close_dt:
            close_dt, close_sample = dt, e
    if close_sample is None:
        return UnmanagedResult(value=None, status="no_data")
    mids = (close_sample.put_short_mid, close_sample.put_long_mid,
            close_sample.call_short_mid, close_sample.call_long_mid)
    if any(m is None for m in mids):
        return UnmanagedResult(value=None, status="no_data")
    spread_value = ((close_sample.put_short_mid - close_sample.put_long_mid)
                    + (close_sample.call_short_mid - close_sample.call_long_mid))
    premium = entry_credit_dollars(entry)
    unmanaged = premium - spread_value * CONTRACT_MULTIPLIER * contracts_of(entry)
    return UnmanagedResult(value=str(unmanaged), status="ok")


def is_provisional(entry: EntryProjection, events: list[Event]) -> bool:
    """EOD-01 PROVISIONAL: settlement not yet captured AND the day hasn't
    been broker-reconciled -- mirrors reports.py's `_summary_settlement_pending`,
    applied per entry rather than per period (an already-reconciled day's
    figures are broker truth regardless of what the bot's own fold captured,
    see that function's docstring)."""
    from meic.reporting.corrections import broker_reconciled_days
    from meic.reporting.folds import entry_day

    if not entry.settlement_pending:
        return False
    return entry_day(entry.entry_id) not in broker_reconciled_days(events)
