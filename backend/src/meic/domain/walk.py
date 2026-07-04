"""Premium walk — STK-02/02a target-premium ceiling selection, STK-03 wing,
STK-07 short-bid validity.

The ceiling is target_premium + target_premium_tolerance (v1.5): a $3.10
short qualifies at a $3.00 target, $3.11 does not (TC-STK-02). Selection
walks toward OTM and takes the FIRST marked strike at or below the ceiling —
with a continuous chain that is the richest qualifying strike; the STK-11
adjacency guard (chain.adjacency_ok) proves the continuity claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .chain import ChainSide, adjacency_ok


@dataclass(frozen=True)
class Selected:
    short_strike: Decimal
    long_strike: Decimal
    short_mid: Decimal


@dataclass(frozen=True)
class Skip:
    reason: str  # "no_valid_strikes" | "incomplete_chain"


@dataclass(frozen=True)
class WingUnmarked:
    """STK-11/STK-07 note: missing wing mark retries within the window rather
    than skipping immediately — the retry policy lives in the application
    layer; the domain just names the condition."""

    short_strike: Decimal
    long_strike: Decimal


def select_side(
    side: ChainSide,
    *,
    target_premium: Decimal,
    tolerance: Decimal,
    wing_width: Decimal,
    otm_direction: Decimal,  # puts: -1 (long below short), calls: +1
) -> Selected | Skip | WingUnmarked:
    """One side's strike selection. Returns the outcome, never raises on
    market conditions. Gates (STK-05/06) run separately on the result."""
    ceiling = target_premium + tolerance
    for strike in side.strikes_toward_otm:
        mark = side.marks.get(strike)
        if mark is None:
            continue  # hole — if it hid a qualifying strike, adjacency catches it
        if mark.mid <= ceiling:
            if mark.bid <= 0:
                return Skip("no_valid_strikes")  # STK-07: short needs a real bid
            if not adjacency_ok(side, strike, ceiling):
                return Skip("incomplete_chain")  # STK-11 -> treated as STK-10 failure
            long_strike = strike + wing_width * otm_direction  # STK-03
            if long_strike not in side.strikes_toward_otm:
                return Skip("no_valid_strikes")  # wing not listed
            if not side.is_marked(long_strike):
                return WingUnmarked(strike, long_strike)  # retry, don't skip yet
            return Selected(strike, long_strike, mark.mid)
    return Skip("no_valid_strikes")  # STK-02: nothing at or below the ceiling
