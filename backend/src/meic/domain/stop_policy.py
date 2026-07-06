"""Stop trigger math + feasibility — STP-02 / STP-02b / STP-02c (pure domain).

This is the code that stands between the account and a runaway short. It only
computes prices and feasibility; placement, verification and escalation live
in the ProtectPosition application service.

STP-02 (v1.39): trigger floors DOWN to the legal tick — contract-preserving.
STP-02b: the operator's rebate markup is added BEFORE tick rounding.
STP-02c: a trigger must clear each short's price by >= min_stop_distance_ticks,
         or the entry is infeasible (a trigger at/below the short is an instant
         exit, not a stop).

`pct` is a whole-number percent from the {95..300 step 5} set (STP-02); the
math divides by 100.
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from .ticks import TickTable


class StopBasis(str, Enum):
    TOTAL_CREDIT = "total_credit"    # DEFAULT (v1.38, Ash's outcome contract)
    SHORT_PREMIUM = "short_premium"  # Rob's formula, per entry
    PER_SIDE = "per_side"


def _raw_trigger(
    basis: StopBasis,
    *,
    pct: Decimal,
    markup: Decimal,
    total_net_credit: Decimal | None,
    short_fill: Decimal | None,
    side_long_fill: Decimal | None,
) -> Decimal:
    p = pct / Decimal(100)
    if basis is StopBasis.TOTAL_CREDIT:
        if total_net_credit is None:
            raise ValueError("total_credit basis needs total_net_credit")
        base = p * total_net_credit
    elif basis is StopBasis.SHORT_PREMIUM:
        if short_fill is None:
            raise ValueError("short_premium basis needs short_fill")
        base = short_fill * (1 + p)
    elif basis is StopBasis.PER_SIDE:
        if short_fill is None or side_long_fill is None:
            raise ValueError("per_side basis needs short_fill and side_long_fill")
        base = short_fill + (short_fill - side_long_fill) * p
    else:  # pragma: no cover - exhaustive
        raise ValueError(f"unknown stop_basis {basis!r}")
    return base + markup  # STP-02b: markup added before tick rounding


def stop_trigger(
    basis: StopBasis,
    *,
    ticks: TickTable,
    pct: Decimal = Decimal("95"),
    markup: Decimal = Decimal("0"),
    total_net_credit: Decimal | None = None,
    short_fill: Decimal | None = None,
    side_long_fill: Decimal | None = None,
) -> Decimal:
    """The stop trigger price, floored to tick (STP-02/02b).

    For TOTAL_CREDIT the result is ONE absolute level shared by both shorts;
    for SHORT_PREMIUM / PER_SIDE it is that side's own trigger.
    """
    raw = _raw_trigger(
        basis, pct=pct, markup=markup, total_net_credit=total_net_credit,
        short_fill=short_fill, side_long_fill=side_long_fill,
    )
    return ticks.floor(raw)


def clears(trigger: Decimal, short_price: Decimal, *, ticks: TickTable, min_distance_ticks: int) -> bool:
    """STP-02c: does `trigger` sit at least min_distance_ticks ABOVE short_price?
    Rule is >= (the knife-edge at exactly the minimum is feasible)."""
    tick = ticks.tick_for(short_price)
    return (trigger - short_price) >= min_distance_ticks * tick


def feasible(
    basis: StopBasis,
    *,
    ticks: TickTable,
    short_prices: dict[str, Decimal],  # side -> short mid (pre-entry) or fill (post-fill)
    pct: Decimal = Decimal("95"),
    markup: Decimal = Decimal("0"),
    total_net_credit: Decimal | None = None,
    side_long_fills: dict[str, Decimal] | None = None,
    min_distance_ticks: int = 2,
) -> bool:
    """STP-02c whole-entry feasibility: EVERY short must be cleared by its own
    trigger. Single-side entries are prohibited, so any short failing kills the
    entry (skip pre-entry / close post-fill — the caller decides which)."""
    for side, short_price in short_prices.items():
        trigger = stop_trigger(
            basis, ticks=ticks, pct=pct, markup=markup,
            total_net_credit=total_net_credit, short_fill=short_price,
            side_long_fill=(side_long_fills or {}).get(side),
        )
        if not clears(trigger, short_price, ticks=ticks, min_distance_ticks=min_distance_ticks):
            return False
    return True
