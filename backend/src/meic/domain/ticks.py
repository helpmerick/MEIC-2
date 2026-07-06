"""Tick rounding — STK-08.

Tick rules are obtained from the API at runtime and INJECTED here; nothing in
the domain hardcodes an instrument's tick structure (STK-08: "obtain tick
rules from the API, don't hardcode"). Tests use SPX's documented structure
($0.05 below $3.00, $0.10 at/above) as fixture data, not as production truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class TickRung:
    """Tick size that applies to prices strictly below `below` (None = catch-all)."""

    below: Decimal | None
    tick: Decimal


@dataclass(frozen=True)
class TickTable:
    """Ordered rungs, ascending thresholds, final rung catch-all (below=None)."""

    rungs: tuple[TickRung, ...]

    def __post_init__(self) -> None:
        if not self.rungs or self.rungs[-1].below is not None:
            raise ValueError("TickTable needs a final catch-all rung (below=None)")

    def tick_for(self, price: Decimal) -> Decimal:
        for rung in self.rungs:
            if rung.below is None or price < rung.below:
                return rung.tick
        raise AssertionError("unreachable: catch-all rung enforced in __post_init__")

    def round(self, price: Decimal) -> Decimal:
        """Round to the nearest tick (half up). STK-08."""
        tick = self.tick_for(price)
        steps = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return (steps * tick).quantize(tick)

    def floor(self, price: Decimal) -> Decimal:
        """Round DOWN to the legal tick. STP-02 (v1.39): stop triggers floor to
        tick — contract-preserving, so a rounding accident can never push the
        both-sides loss beyond the promise."""
        tick = self.tick_for(price)
        steps = (price / tick).quantize(Decimal("1"), rounding=ROUND_FLOOR)
        return (steps * tick).quantize(tick)
