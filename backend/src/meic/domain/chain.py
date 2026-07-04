"""Chain snapshot model + integrity gates — STK-04 (data), STK-10, STK-11.

Pure: the snapshot is handed in fully formed (adapters own staleness stamping
and retrieval). One ChainSide = one option type's view for one expiration.
Strikes toward-OTM ordering is the caller's responsibility (puts: descending
from the money; calls: ascending) so this module never needs to know spot
conventions — it just walks the given order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


@dataclass(frozen=True)
class Mark:
    """Two-sided quote for one strike. A strike with no Mark is a hole."""

    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class ChainSide:
    """One option type's strikes for one expiration.

    strikes_toward_otm: ALL listed strikes, ordered nearest-the-money first.
    marks: only strikes with valid two-sided marks appear.
    """

    strikes_toward_otm: tuple[Decimal, ...]
    marks: Mapping[Decimal, Mark]

    def is_marked(self, strike: Decimal) -> bool:
        return strike in self.marks

    def one_step_closer_to_money(self, strike: Decimal) -> Decimal | None:
        """The adjacent listed strike toward the money (STK-11 guard input)."""
        idx = self.strikes_toward_otm.index(strike)
        return self.strikes_toward_otm[idx - 1] if idx > 0 else None


def completeness_ok(
    side: ChainSide,
    *,
    band_strikes: tuple[Decimal, ...],
    completeness_pct: Decimal,
) -> bool:
    """STK-10 completeness gate for one option type.

    band_strikes: the listed strikes inside ±chain_atm_band_pts of spot
    (the caller computes the band from spot; this stays spot-agnostic).
    Far-OTM strikes outside the band never trip the gate (TC-STK-07).
    """
    if not band_strikes:
        return False
    marked = sum(1 for s in band_strikes if side.is_marked(s))
    return (Decimal(marked) / Decimal(len(band_strikes))) * 100 >= completeness_pct


def adjacency_ok(side: ChainSide, selected: Decimal, ceiling: Decimal) -> bool:
    """STK-11 selection-continuity guard.

    The strike one step closer to the money than the selection must (a) have a
    valid mark and (b) carry a premium ABOVE the ceiling — proving the walk
    descended continuously rather than leaping a hole. The nearest-the-money
    strike (no closer neighbour) passes vacuously.
    """
    closer = side.one_step_closer_to_money(selected)
    if closer is None:
        return True
    mark = side.marks.get(closer)
    return mark is not None and mark.mid > ceiling
