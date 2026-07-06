"""Chain snapshot model + integrity gate — STK-04 (data), STK-10.

Pure: the snapshot is handed in fully formed (adapters own staleness stamping
and retrieval). One ChainSide = one option type's view for one expiration.
Strikes toward-OTM ordering is the caller's responsibility (puts: descending
from the money; calls: ascending) so this module never needs to know spot
conventions.

v1.39 note: the v1.4 adjacency guard is retired — STK-11 is now probe-match
integrity, enforced inside the probe walk itself (walk.py); STK-10
completeness remains the primary defense against holey chains.
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
