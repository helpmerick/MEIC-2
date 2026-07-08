"""LiveCondorSelector — turn a live chain snapshot into a Condor, or a skip reason.

Composes already-tested pure domain pieces in the spec's order:
  DAT-02 freshness -> STK-10 completeness -> probe walk (STK-02/11) ->
  STK-09 collisions -> credit gates re-run on the FINAL strikes (STK-05/06).

Every failure returns a NAMED skip reason and no Condor: the runtime then skips
the entry. Nothing here can produce a partially-valid selection — a missing mark
on any final leg is a skip, not a guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Awaitable, Callable, Mapping

from meic.application.execute_entry import Condor
from meic.domain.chain import ChainSide, completeness_ok
from meic.domain.collision import Abort, Resolved, resolve_collisions
from meic.domain.gates import GatesFailed, check_credit_gates
from meic.domain.walk import Selected, Skip, WingUnmarked, select_side

Occupancy = Mapping[Decimal, frozenset]


@dataclass(frozen=True)
class SelectionConfig:
    target_premium: Decimal = Decimal("3.00")
    wing_width: Decimal = Decimal("50")
    min_short_premium: Decimal = Decimal("1.00")
    min_total_credit: Decimal = Decimal("2.00")
    completeness_pct: Decimal = Decimal("90")


@dataclass
class LiveCondorSelector:
    snapshot_provider: Callable[[], Awaitable]        # () -> ChainSnapshot
    config: SelectionConfig = SelectionConfig()
    occupancy_provider: Callable[[], Occupancy] = dict

    def _side(self, chain: ChainSide, direction: Decimal):
        c = self.config
        return select_side(chain, target_premium=c.target_premium, wing_width=c.wing_width,
                           otm_direction=direction, min_short_premium=c.min_short_premium)

    def _resolve(self, sel: Selected, chain: ChainSide, direction: Decimal, occ: Occupancy):
        return resolve_collisions(
            short_strike=sel.short_strike, long_strike=sel.long_strike, occupancy=occ,
            listed_strikes_toward_otm=chain.strikes_toward_otm,
            wing_width=self.config.wing_width, otm_direction=direction)

    async def __call__(self, when: datetime, entry_number: int) -> tuple[Condor | None, str | None]:
        c = self.config
        snap = await self.snapshot_provider()

        if snap.stale:                                    # DAT-02: never trade stale data
            return None, "data_unavailable"

        # STK-10: a holey ATM band means no selection at all (both types)
        for chain, band in ((snap.put_side, snap.put_band), (snap.call_side, snap.call_band)):
            if not completeness_ok(chain, band_strikes=band, completeness_pct=c.completeness_pct):
                return None, "incomplete_chain"

        put = self._side(snap.put_side, Decimal(-1))
        call = self._side(snap.call_side, Decimal(1))
        for r in (put, call):
            if isinstance(r, Skip):
                return None, r.reason
            if isinstance(r, WingUnmarked):
                return None, "wing_unmarked"             # retry policy is the runtime's

        occ = self.occupancy_provider() or {}
        legs: dict[str, Resolved] = {}
        for name, sel, chain, direction in (
            ("put", put, snap.put_side, Decimal(-1)),
            ("call", call, snap.call_side, Decimal(1)),
        ):
            resolved = self._resolve(sel, chain, direction, occ)
            if isinstance(resolved, Abort):
                return None, resolved.reason             # strike_collision
            legs[name] = resolved

        # Every FINAL leg must carry a real mark — a shifted strike without one
        # is a skip, never an estimate.
        mids: dict[str, Decimal] = {}
        for name, chain in (("put", snap.put_side), ("call", snap.call_side)):
            for role, strike in (("short", legs[name].short_strike), ("long", legs[name].long_strike)):
                mark = chain.marks.get(strike)
                if mark is None:
                    return None, "wing_unmarked" if role == "long" else "no_valid_strikes"
                mids[f"{name}_{role}"] = mark.mid

        net_credit = ((mids["put_short"] - mids["put_long"]) +
                      (mids["call_short"] - mids["call_long"]))

        # STK-05/06 re-run on the final (possibly shifted) strikes
        gate = check_credit_gates(
            put_short_mid=mids["put_short"], call_short_mid=mids["call_short"],
            total_net_credit_mid=net_credit,
            min_short_premium=c.min_short_premium, min_total_credit=c.min_total_credit)
        if isinstance(gate, GatesFailed):
            return None, gate.reason

        return Condor(
            entry_number=entry_number,
            put_short=legs["put"].short_strike, call_short=legs["call"].short_strike,
            # STK-03 wings — the ACL needs all four strikes to build the order.
            put_long=legs["put"].long_strike, call_long=legs["call"].long_strike,
            put_short_mid=mids["put_short"], call_short_mid=mids["call_short"],
            mid_credit=net_credit, min_total_credit=c.min_total_credit,
            expiration=when.date(),  # 0DTE
        ), None
