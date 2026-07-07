"""Net-loss preview — NLE-05 / UI-13 (pure, informational).

The stop_loss_pct selector shows a LIVE per-side net-loss preview for a
candidate pct using the current chain. It is a pure recompute: it submits
nothing and changes no state, so the operator can scrub the selector freely.
When the market is closed or the chain is stale the preview is UNAVAILABLE —
never a stale or fabricated number.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from meic.domain.nle import EstimateUnavailable, estimate_net_loss
from meic.domain.stop_policy import StopBasis, stop_trigger
from meic.domain.ticks import TickTable


@dataclass(frozen=True)
class SideInput:
    chain_mids: Mapping[Decimal, Decimal]
    short_strike: Decimal
    short_fill: Decimal
    long_strike: Decimal
    long_fill: Decimal


def preview_net_loss(
    *,
    pct: Decimal,
    ticks: TickTable,
    market_open: bool,
    data_fresh: bool,
    put: SideInput,
    call: SideInput,
    total_net_credit: Decimal,
    nle_haircut_pct: Decimal = Decimal("30"),
) -> dict:
    """Per-side net-loss estimates for a candidate pct (total_credit basis).
    Returns {"available": False, "reason": ...} when the market is closed or the
    chain is stale; otherwise {"available": True, "trigger": ..., "put": ...,
    "call": ...}. Submits nothing (UI-13)."""
    if not market_open:
        return {"available": False, "reason": "market_closed"}
    if not data_fresh:
        return {"available": False, "reason": "stale_chain"}

    # one shared trigger level for total_credit (both shorts share it)
    trigger = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=ticks, pct=pct,
                           total_net_credit=total_net_credit)

    def _side(s: SideInput):
        return estimate_net_loss(
            chain_mids=s.chain_mids, short_strike=s.short_strike, short_fill=s.short_fill,
            long_strike=s.long_strike, long_fill=s.long_fill, stop_trigger=trigger,
            nle_haircut_pct=nle_haircut_pct)

    return {"available": True, "trigger": trigger, "put": _side(put), "call": _side(call)}
