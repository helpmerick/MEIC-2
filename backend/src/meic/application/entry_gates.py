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

from meic.domain.risk import exceeds_max_day_risk


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
        # Coarse: is BP readable and the account unrestricted? The exact
        # "sufficient for the worst-case margin of the NEW condor" comparison
        # needs the condor, so it lives in evaluate_risk below — same rule, same
        # reason, evaluated where the number is known.
        return "insufficient_bp"
    return None


# --- the risk rails every entry crosses --------------------------------------
# ENT-09 spells the full chain: "... session valid ∧ buying power ∧ order cap ∧
# RSK-04". The first eight are evaluate_gates above; these are the last two.

@dataclass(frozen=True)
class RiskSnapshot:
    """RSK-08 + RSK-04 inputs, evaluated on the SHARED entry path so a manual
    ENT-09 fire crosses these rails identically to a scheduled one."""

    new_worst_case: Decimal                       # (width − credit) × 100 × contracts
    open_worst_cases: tuple[Decimal, ...] = ()    # one per already-open entry
    max_day_risk: Decimal | None = None           # config; mandatory before live (doc 06 §169)
    order_cap_allows_entry: bool = True           # RSK-08 (exit-side orders are never capped)
    # ENT-03's BP gate is "buying power sufficient for worst-case margin OF THE NEW
    # CONDOR" — a comparison, not a flag. GateSnapshot.buying_power_ok can only
    # answer the coarse question (is BP readable, is the account unrestricted),
    # because it is built before the condor is priced. The number lives here, where
    # `new_worst_case` is. None = don't check (offline days / tests).
    buying_power: Decimal | None = None
    # In paper this is the SimLedger's BP (SIM-04: the gate strains against
    # simulated capital exactly as live); in live, derivative_buying_power.


def evaluate_risk(r: RiskSnapshot) -> str | None:
    """Return the first failing risk rail's skip reason, or None.

    Order per ENT-09: "... session valid ∧ buying power ∧ order cap ∧ RSK-04".
    RSK-04 goes last because it is the only rail needing the whole day's exposure.
    `max_day_risk` is mandatory before live mode can be enabled (doc 06 §169), so
    None here means paper/tests — never "unlimited in production".
    """
    if r.buying_power is not None and r.buying_power < r.new_worst_case:
        return "insufficient_bp"          # ENT-03 pre-trade; EC-ENT-07 is the broker's own
    if not r.order_cap_allows_entry:
        return "order_cap"                # RSK-08 daily order-count rail
    if r.max_day_risk is not None and exceeds_max_day_risk(
            list(r.open_worst_cases), r.new_worst_case, r.max_day_risk):
        return "max_day_risk"             # RSK-04 — Σ(per-entry worst cases) + this one
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
    # CAL-05 (v1.71): the calendar tag store's NO-TRADE label for the ET
    # trading day this attempt falls on, or None if untagged. Populated by
    # the CALLER (application/calendar_store.py's CalendarStore.label_for_day,
    # itself fail-open per CAL-07) — this module stays pure/IO-free, exactly
    # like every other FilterSnapshot field. Additive alongside the pre-v1.71
    # `skip_dates` static list below: that path is unchanged, never replaced.
    blackout_label: str | None = None


# Skips from optional filters are info-level (ENT-06 / TC-ENT-04) — an expected,
# non-alarming "not today", not a failure.
class _SkipLevels(dict):
    """CAL-05 (v1.71): the exact-match keys below don't cover the DYNAMIC
    `blackout:<label>` reasons `evaluate_filters` now returns (the label
    varies per tag, so it can never be a fixed key) — `__missing__`
    classifies those info-level, identical to the static `blackout_date`
    they sit beside, so a future consumer can never misclassify a calendar
    blackout as alarming. Any OTHER unknown reason still raises KeyError,
    never a silently-guessed level."""

    def __missing__(self, reason):
        if isinstance(reason, str) and reason.startswith("blackout:"):
            return "info"    # CAL-05 calendar blackout — expected, non-alarming
        raise KeyError(reason)


SKIP_LEVEL = _SkipLevels({
    "vix_above_max": "info",
    "blackout_date": "info",
    "below_min_credit": "info",
})


def evaluate_filters(f: FilterSnapshot) -> str | None:
    """ENT-06: checked at ENT-03 time, each filter independently toggleable.
    Returns the first triggered filter's skip reason (info-level), else None.

    CAL-05 (v1.71): the calendar tag store is checked first, ahead of the
    pre-existing static `skip_dates` list -- both are additive blackout
    sources (the static list is UNCHANGED: same field, same trigger
    condition, same `blackout_date` reason string), and either alone is
    sufficient to skip the entry."""
    if f.vix_max is not None and f.vix is not None and f.vix > f.vix_max:
        return "vix_above_max"            # skip, info-level (ENT-06)
    if f.blackout_label is not None:
        return f"blackout:{f.blackout_label}"   # CAL-05: calendar tag store
    if f.date is not None and f.date in f.skip_dates:
        return "blackout_date"            # pinned pre-v1.71 path, additive
    if (f.min_total_credit is not None and f.total_credit is not None
            and f.total_credit < f.min_total_credit):
        return "below_min_credit"         # STK-06
    return None


def clock_drift_blocks_entry(*, drift_ms: float, max_drift_ms: float) -> bool:
    """RSK-07/DAY-03: clock drift beyond the configured max blocks NEW entries.
    Resting stops and in-flight management are UNAFFECTED — they live at the
    broker, which is why this is an entry-only gate."""
    return abs(drift_ms) > max_drift_ms
