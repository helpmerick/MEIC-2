"""ENT-03 pre-entry gate chain (pure decision).

Before each entry attempt the bot verifies, IN ORDER (ENT-03): ARMED,
Confirm Live ON, Stop Trading off, no Flatten All executing, market open and
not halted, market data fresh, broker session valid, buying power sufficient.
The first failure names the skip reason and the entry is skipped — never
executed partway.

Pure: evaluates a snapshot and returns the reason (or None). The application
service takes the snapshot from PersistentState + market/broker adapters and
acts on the verdict.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class GateSnapshot:
    armed: bool
    confirm_live: bool
    stop_trading: bool
    flatten_in_progress: bool
    market_open: bool
    market_halted: bool
    data_fresh: bool
    session_valid: bool
    buying_power_ok: bool


def evaluate_gates(s: GateSnapshot) -> str | None:
    """Return the first failing gate's skip reason, or None if all pass.
    Order and reasons are ENT-03 / TC-ENT-02 canonical."""
    if not s.armed:
        return "disarmed"                 # ENT-01a
    if not s.confirm_live:
        return "confirm_live_off"         # ENT-01b
    if s.stop_trading:
        return "stop_trading"             # RSK-01
    if s.flatten_in_progress:
        return "flatten_in_progress"      # RSK-01a
    if not s.market_open or s.market_halted:
        return "market_halted"            # DAT-04
    if not s.data_fresh:
        return "data_unavailable"         # DAT-02
    if not s.session_valid:
        return "invalid_session"          # broker session
    if not s.buying_power_ok:
        return "insufficient_bp"          # worst-case margin
    return None


# --- ENT-06 optional filters (each independently toggleable; None = off) ------

@dataclass(frozen=True)
class FilterSnapshot:
    vix: Decimal | None = None
    vix_max: Decimal | None = None
    date: str | None = None
    skip_dates: tuple[str, ...] = ()
    total_credit: Decimal | None = None
    min_total_credit: Decimal | None = None


# Skips from optional filters are info-level (ENT-06 / TC-ENT-04) — an expected,
# non-alarming "not today", not a failure.
SKIP_LEVEL = {
    "vix_above_max": "info",
    "blackout_date": "info",
    "below_min_credit": "info",
}


def evaluate_filters(f: FilterSnapshot) -> str | None:
    """ENT-06: checked at ENT-03 time, each filter independently toggleable.
    Returns the first triggered filter's skip reason (info-level), else None."""
    if f.vix_max is not None and f.vix is not None and f.vix > f.vix_max:
        return "vix_above_max"            # skip, info-level (ENT-06)
    if f.date is not None and f.date in f.skip_dates:
        return "blackout_date"            # explicit blackout (e.g. FOMC)
    if (f.min_total_credit is not None and f.total_credit is not None
            and f.total_credit < f.min_total_credit):
        return "below_min_credit"         # STK-06
    return None


def clock_drift_blocks_entry(*, drift_ms: float, max_drift_ms: float) -> bool:
    """RSK-07/DAY-03: clock drift beyond the configured max blocks NEW entries.
    Resting stops and in-flight management are UNAFFECTED — they live at the
    broker, which is why this is an entry-only gate."""
    return abs(drift_ms) > max_drift_ms
