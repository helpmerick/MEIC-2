"""Standing entry schedule — UC-02 composition + validation (pure domain, v1.44).

Each row is an EntrySpec: a time plus OPTIONAL per-entry overrides. Unset fields
inherit the global value (doc 06 §37); **validation runs per entry AFTER
inheritance**, exactly as the spec requires.

A bad schedule is a real-money hazard — an entry 30 seconds before the close, a
$0.10 premium target, or 11 contracts must be UN-ARM-ABLE. So validation returns
every offending field, named, rather than raising on the first.

Purity: session bounds (open/close) are passed in. The domain never asks a
calendar what day it is.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time, timedelta
from decimal import Decimal
from typing import Iterable

STOP_PCT_SET = frozenset(range(95, 305, 5))          # {95,100,…,300} exactly (STP-02)
SELECTABLE_STOP_BASES = ("total_credit", "short_premium")  # per_side gated (STP-02d)
STRIKE_METHODS = ("premium", "delta")


@dataclass(frozen=True)
class ScheduleDefaults:
    """Global config values a row inherits when it does not override them."""
    contracts: int = 1                              # contracts_per_entry: row pre-fill (v1.44)
    target_premium: Decimal = Decimal("3.00")
    wing_width: Decimal = Decimal("50")
    stop_loss_pct: int = 95
    stop_basis: str = "total_credit"
    stop_rebate_markup: Decimal = Decimal("0.00")
    min_short_premium: Decimal = Decimal("1.00")
    min_total_credit: Decimal = Decimal("2.00")
    probe_down_max: int = 25
    strike_method: str = "premium"
    short_delta_target: Decimal = Decimal("0.10")


@dataclass(frozen=True)
class EntrySpec:
    """One schedule row. `time` is ET. Everything else is an optional override."""
    time: time
    contracts: int | None = None
    target_premium: Decimal | None = None
    wing_width: Decimal | None = None
    stop_loss_pct: int | None = None
    stop_basis: str | None = None
    stop_rebate_markup: Decimal | None = None
    min_short_premium: Decimal | None = None
    min_total_credit: Decimal | None = None
    probe_down_max: int | None = None
    strike_method: str | None = None
    short_delta_target: Decimal | None = None


@dataclass(frozen=True)
class ResolvedEntry:
    """An EntrySpec with every field concrete (post-inheritance)."""
    time: time
    contracts: int
    target_premium: Decimal
    wing_width: Decimal
    stop_loss_pct: int
    stop_basis: str
    stop_rebate_markup: Decimal
    min_short_premium: Decimal
    min_total_credit: Decimal
    probe_down_max: int
    strike_method: str
    short_delta_target: Decimal


@dataclass(frozen=True)
class ScheduleError:
    """A named validation failure. `index` is the row (None => schedule-level)."""
    field: str
    reason: str
    index: int | None = None


_OVERRIDABLE = (
    "contracts", "target_premium", "wing_width", "stop_loss_pct", "stop_basis",
    "stop_rebate_markup", "min_short_premium", "min_total_credit",
    "probe_down_max", "strike_method", "short_delta_target",
)


def resolve(entry: EntrySpec, defaults: ScheduleDefaults) -> ResolvedEntry:
    """Doc 06 §37: unset fields inherit the global value."""
    values = {name: (getattr(entry, name) if getattr(entry, name) is not None
                     else getattr(defaults, name))
              for name in _OVERRIDABLE}
    return ResolvedEntry(time=entry.time, **values)


def _is_step(value: Decimal, step: str) -> bool:
    return (value / Decimal(step)) % 1 == 0


def validate_entry(e: ResolvedEntry, index: int) -> list[ScheduleError]:
    """Per-entry ranges, applied AFTER inheritance (doc 06)."""
    errs: list[ScheduleError] = []

    def bad(field: str, reason: str) -> None:
        errs.append(ScheduleError(field=field, reason=reason, index=index))

    if not 1 <= e.contracts <= 10:                                    # ENT-04 (v1.44)
        bad("contracts", "out_of_range")
    if not Decimal("0.50") <= e.target_premium <= Decimal("20.00"):   # STK-02
        bad("target_premium", "out_of_range")
    if not (Decimal("10") <= e.wing_width <= Decimal("200")):         # STK-03
        bad("wing_width", "out_of_range")
    elif not _is_step(e.wing_width, "5"):
        bad("wing_width", "bad_step")
    if e.stop_loss_pct not in STOP_PCT_SET:                           # STP-02 / UI-04
        bad("stop_loss_pct", "not_in_set")
    if e.stop_basis == "per_side":                                    # STP-02d gate
        bad("stop_basis", "allocation_unverified")
    elif e.stop_basis not in SELECTABLE_STOP_BASES:
        bad("stop_basis", "not_in_set")
    if not (Decimal("0.00") <= e.stop_rebate_markup <= Decimal("5.00")):  # STP-02b
        bad("stop_rebate_markup", "out_of_range")
    elif not _is_step(e.stop_rebate_markup, "0.05"):
        bad("stop_rebate_markup", "bad_step")
    if not Decimal("0.05") <= e.min_short_premium <= Decimal("20.00"):    # STK-05
        bad("min_short_premium", "out_of_range")
    if not Decimal("0.10") <= e.min_total_credit <= Decimal("40.00"):     # STK-06
        bad("min_total_credit", "out_of_range")
    if not 1 <= e.probe_down_max <= 40:                                   # STK-02 (v1.44)
        bad("probe_down_max", "out_of_range")
    if e.strike_method not in STRIKE_METHODS:
        bad("strike_method", "not_in_set")
    if not Decimal("0.03") <= e.short_delta_target <= Decimal("0.30"):    # STK-02
        bad("short_delta_target", "out_of_range")
    return errs


def validate_schedule(
    entries: Iterable[EntrySpec],
    defaults: ScheduleDefaults,
    *,
    session_open: time,
    session_close: time,
    min_time_before_close_minutes: int = 30,
) -> list[ScheduleError]:
    """Doc 06 validation rules 1–3 plus per-entry ranges. Returns EVERY error.

    Schedule-level (rule 3): times strictly increasing, inside market hours, and
    each at least `min_time_before_close` before the (possibly early) close.
    """
    rows = list(entries)
    errs: list[ScheduleError] = []

    if not rows:  # ENT-01a: arming an empty schedule is rejected
        return [ScheduleError(field="entries", reason="empty_schedule")]

    latest = (
        (timedelta(hours=session_close.hour, minutes=session_close.minute)
         - timedelta(minutes=min_time_before_close_minutes))
    )
    latest_time = (timedelta(0) + latest)

    previous: time | None = None
    for i, row in enumerate(rows):
        errs.extend(validate_entry(resolve(row, defaults), i))

        if previous is not None and row.time <= previous:  # strictly increasing
            errs.append(ScheduleError(field="time", reason="not_strictly_increasing", index=i))
        previous = row.time

        if not (session_open <= row.time < session_close):  # inside market hours
            errs.append(ScheduleError(field="time", reason="outside_market_hours", index=i))
            continue

        as_delta = timedelta(hours=row.time.hour, minutes=row.time.minute)
        if as_delta > latest_time:  # >= min_time_before_close ahead of the close
            errs.append(ScheduleError(field="time", reason="too_close_to_close", index=i))

    return errs


def may_arm(entries: Iterable[EntrySpec], defaults: ScheduleDefaults, **kw) -> bool:
    """ENT-01a: arming requires >= 1 entry AND a fully valid schedule."""
    return not validate_schedule(entries, defaults, **kw)
