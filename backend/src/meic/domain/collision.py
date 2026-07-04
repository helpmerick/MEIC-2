"""Strike collision avoidance — STK-09 (Ash's rules, ratified v1.27).

Same type stacks (short-on-short, long-on-long: no shift). Opposite type
blocks (short blocked by longs, long blocked by shorts), including in-flight
working orders for opposite-type checks only. Short budget: 3 strikes total
(original + max_strike_shifts=2), wing follows, then abort. Long budget:
max_long_shifts=5 solo shifts (spread widens; RSK-04 gates the widened worst
case — application layer), then abort. Sides are judged independently.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

# Occupancy: strike -> set of {"short", "long"} covering positions AND
# in-flight working orders (in-flight matters for opposite-type checks only,
# and the caller builds the map accordingly — same-type in-flight never blocks).
Occupancy = Mapping[Decimal, frozenset[str]]


@dataclass(frozen=True)
class Resolved:
    short_strike: Decimal
    long_strike: Decimal
    short_shifts: int
    long_shifts: int

    @property
    def widened(self) -> bool:
        """True when the long shifted alone — RSK-04 must re-gate the worst case."""
        return self.long_shifts > 0


@dataclass(frozen=True)
class Abort:
    reason: str  # always "strike_collision"


def _blocked(occupancy: Occupancy, strike: Decimal, by: str) -> bool:
    return by in occupancy.get(strike, frozenset())


def resolve_collisions(
    *,
    short_strike: Decimal,
    long_strike: Decimal,
    occupancy: Occupancy,
    listed_strikes_toward_otm: tuple[Decimal, ...],
    wing_width: Decimal,
    otm_direction: Decimal,
    max_strike_shifts: int = 2,
    max_long_shifts: int = 5,
) -> Resolved | Abort:
    """Apply STK-09 to one side's proposed strikes. Credit gates re-run on the
    result (caller's job — TC-STK-06 'Gates re-run on final strikes')."""
    # --- short: blocked by existing LONGs; shifts OTM with wing following ---
    short_shifts = 0
    s = short_strike
    while _blocked(occupancy, s, "long"):
        short_shifts += 1
        if short_shifts > max_strike_shifts:
            return Abort("strike_collision")  # 3 strikes total, then abort
        s = s + _step(listed_strikes_toward_otm, s, otm_direction)
        if s not in listed_strikes_toward_otm:
            return Abort("strike_collision")
    w = s + wing_width * otm_direction  # wing follows the short (width preserved)

    # --- long: blocked by existing SHORTs; shifts alone, spread widens ---
    long_shifts = 0
    while _blocked(occupancy, w, "short"):
        long_shifts += 1
        if long_shifts > max_long_shifts:
            return Abort("strike_collision")
        w = w + _step(listed_strikes_toward_otm, w, otm_direction)
        if w not in listed_strikes_toward_otm:
            return Abort("strike_collision")
    return Resolved(s, w, short_shifts, long_shifts)


def _step(listed: tuple[Decimal, ...], from_strike: Decimal, otm_direction: Decimal) -> Decimal:
    """Distance to the next listed strike further OTM."""
    try:
        idx = listed.index(from_strike)
    except ValueError:
        # already off the listed ladder: signal via a step that leaves the ladder
        return otm_direction * Decimal("1000000")
    if idx + 1 >= len(listed):
        return otm_direction * Decimal("1000000")
    return listed[idx + 1] - from_strike
