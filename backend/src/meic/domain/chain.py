"""Chain snapshot model + integrity gate — STK-04 (data), STK-10.

Pure: the snapshot is handed in fully formed (adapters own staleness stamping
and retrieval). One ChainSide = one option type's view for one expiration.
Strikes toward-OTM ordering is the caller's responsibility (puts: descending
from the money; calls: ascending) so this module never needs to know spot
conventions.

v1.39 note: the v1.4 adjacency guard is retired — STK-11 is now probe-match
integrity, enforced inside the probe walk itself (walk.py); STK-10
completeness remains the primary defense against holey chains.

v1.51 note: the FIXED `chain_atm_band_pts` band is retired (it couldn't track
the moving far-OTM dead-strike boundary). STK-10 now gates on the entry's own
TRADE-RELATIVE reachable strike set (`reachable_strikes`, below) — reused from
walk.py's probe-window floor/lattice rounding and collision.py's "next listed
strike further OTM" stepping so this module never re-derives either.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .collision import _step
# NOTE: walk.py imports ChainSide from THIS module, so `_LATTICE`/`lattice_price`
# are imported lazily inside `reachable_strikes` below to avoid a circular
# module-load-time import (chain -> walk -> chain).


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
    reachable: frozenset[Decimal] | tuple[Decimal, ...],
    completeness_pct: Decimal,
) -> bool:
    """STK-10 v1.51 completeness gate for one option type.

    `reachable`: the entry's TRADE-RELATIVE reachable strike set (see
    `reachable_strikes` below) — never a fixed ATM band. Far-OTM listed strikes
    outside this set never trip the gate (TC-STK-07: the v1.51 regression).
    """
    if not reachable:
        return False
    marked = sum(1 for s in reachable if side.is_marked(s))
    return (Decimal(marked) / Decimal(len(reachable))) * 100 >= completeness_pct


def reachable_strikes(
    side: ChainSide,
    *,
    target_premium: Decimal,
    wing_width: Decimal,
    otm_direction: Decimal,  # puts: -1, calls: +1 (matches walk.select_side)
    min_short_premium: Decimal = Decimal("1.00"),
    probe_up_max: int = 3,
    probe_down_max: int = 25,
    max_strike_shifts: int = 2,
    max_long_shifts: int = 5,
) -> frozenset[Decimal]:
    """STK-10 v1.51 TRADE-RELATIVE reachable strike set for one option type.

    The set of LISTED strikes this entry's OWN parameters could possibly
    touch, computed WITHOUT knowing which strikes are marked/occupied (the
    completeness gate must be evaluable before selection runs):

      (a) every MARKED strike whose rounded mid falls in the probe premium
          window [max(target-1.25, min_short_premium), target+probe_up cap] —
          i.e. every strike the probe walk (walk.py) could match as a short.
          The floor/lattice rounding here are walk.py's own — never re-derived.
      (b) the wing strike (short +/- wing_width, per side) for each strike
          from (a).
      (c) the STK-09 (collision.py) shift budgets: for each (a) short, the
          next `max_strike_shifts` listed strikes further OTM WITH their own
          wings; and for every wing counted so far, the next `max_long_shifts`
          listed strikes further OTM (long solo shifts). "Next listed strike
          further OTM" reuses collision.py's own stepping (`_step`).

    Strikes from (b)/(c) may be UNMARKED — that's the point: an unmarked wing
    (or shift target) counts against completeness upfront, instead of
    surviving to a later `wing_unmarked` surprise. A far-OTM listed-but-dead
    strike that never falls in this set never affects STK-10.
    """
    from .walk import _LATTICE, lattice_price  # local: avoids chain<->walk circular import

    floor = max(target_premium - _LATTICE * probe_down_max, min_short_premium)
    ceiling = target_premium + _LATTICE * probe_up_max
    listed = side.strikes_toward_otm

    reachable_shorts = {
        strike for strike in listed
        if (mark := side.marks.get(strike)) is not None
        and floor <= lattice_price(mark.mid) <= ceiling
    }

    result: set[Decimal] = set(reachable_shorts)

    def _next_otm(strike: Decimal) -> Decimal | None:
        nxt = strike + _step(listed, strike, otm_direction)
        return nxt if nxt in listed else None

    for short in reachable_shorts:
        # (c) short-shift budget: the original short plus up to max_strike_shifts
        # further-OTM listed strikes, each carrying its own wing.
        shorts_with_wings = [short]
        s = short
        for _ in range(max_strike_shifts):
            nxt = _next_otm(s)
            if nxt is None:
                break
            shorts_with_wings.append(nxt)
            result.add(nxt)
            s = nxt

        for shifted_short in shorts_with_wings:
            wing = shifted_short + wing_width * otm_direction  # (b)
            if wing not in listed:
                continue  # not listed: cannot count either way
            result.add(wing)
            # (c) long-solo-shift budget: this wing plus up to max_long_shifts
            # further-OTM listed strikes, the short held fixed.
            w = wing
            for _ in range(max_long_shifts):
                nxt = _next_otm(w)
                if nxt is None:
                    break
                result.add(nxt)
                w = nxt

    return frozenset(result)
