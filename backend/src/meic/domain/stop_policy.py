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

from decimal import ROUND_HALF_UP, Decimal
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


def markup_worst_case_increase(markup: Decimal, *, contracts: int = 1) -> Decimal:
    """UI-18: the worst-case extra loss a rebate markup can cause. The markup
    raises each short's stop by `markup`, so a stop-out pays that much more per
    contract (×100); both sides stopping is the worst case (×2)."""
    return markup * 100 * contracts * 2


def effective_stop_pct(trigger: Decimal, net_credit: Decimal) -> Decimal:
    """STP-02b effective-percentage cage (v1.67): the ACTUAL stop trigger as a
    percentage of the entry's net credit -- computed AFTER tick-flooring, not
    from the raw pre-floor figure (the precision trap TC-STP-21 pins: credit
    2.80, markup 0.30 -> raw 2.96 floors to 2.95 -> effective 105.4%, not the
    raw 105.7%). A fixed-dollar markup's bite scales inversely with credit; this
    is the number that makes that visible instead of discovered on a bad day.

    Rounded to one decimal place (ROUND_HALF_UP) -- the precision the spec's
    own examples are stated in (105.4, 110.0)."""
    if net_credit <= 0:
        raise ValueError("net_credit must be > 0 to compute an effective stop percentage")
    pct = (trigger / net_credit) * 100
    return pct.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def within_effective_cap(effective_pct: Decimal, cap_pct: Decimal) -> bool:
    """STP-02b cage: the cap is inclusive of its own boundary (110 exactly is
    ALLOWED, per TC-STP-21) -- only strictly exceeding it skips the entry."""
    return effective_pct <= cap_pct


def effective_cap_check(
    basis: StopBasis,
    *,
    ticks: TickTable,
    short_prices: dict[str, Decimal],
    net_credit: Decimal,
    pct: Decimal = Decimal("95"),
    markup: Decimal = Decimal("0"),
    side_long_fills: dict[str, Decimal] | None = None,
    cap_pct: Decimal = Decimal("110"),
) -> tuple[bool, Decimal]:
    """STP-02b whole-entry cage: computes every short's trigger (identically to
    `feasible()`), each side's effective stop % against the entry's OWN net
    credit, and whether the WORST (highest) side clears `cap_pct`. One
    computation feeds both the gate (`ok`) and the display (`worst_effective_pct`)
    so they can never diverge -- the cage SKIPS (reason `markup_exceeds_cap`),
    it never clamps the markup to fit."""
    worst = Decimal("0")
    for side, short_price in short_prices.items():
        trigger = stop_trigger(
            basis, ticks=ticks, pct=pct, markup=markup,
            total_net_credit=net_credit, short_fill=short_price,
            side_long_fill=(side_long_fills or {}).get(side),
        )
        eff = effective_stop_pct(trigger, net_credit)
        worst = max(worst, eff)
    return within_effective_cap(worst, cap_pct), worst


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
