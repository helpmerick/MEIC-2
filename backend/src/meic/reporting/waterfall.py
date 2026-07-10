"""RPT-07/RPT-11 P&L attribution waterfall.

Total credit collected -> - stop-out costs -> + long recoveries -> -
close/decay buybacks -> - fees -> - net slippage -> = Net P&L. MUST reconcile
to the cent; a residual renders an explicit ERROR STATE, never a silently
adjusted bar (doc 10 RPT-11 / TC-RPT-07).

Pinned vector (TC-RPT-07): credits 8400, stop costs 2600, recoveries 310,
buybacks 145, fees 220, slippage 95 => net 5650 (8400-2600+310-145-220-95),
premium capture 5650/8400 = 67.3% (@1dp; exact fraction returned here).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class WaterfallResidualError(Exception):
    """RPT-11: the waterfall failed to reconcile to the cent against an
    independently-known net (e.g. the projection's own folded P&L). This is
    an explicit error state — the whole point of RPT-11's rule is that a
    residual is surfaced, never quietly absorbed into one of the bars."""

    def __init__(self, expected_net: Decimal, computed_net: Decimal) -> None:
        self.expected_net = expected_net
        self.computed_net = computed_net
        self.residual = computed_net - expected_net
        super().__init__(
            f"waterfall residual {self.residual} (computed net {computed_net} != "
            f"expected {expected_net}) -- reconciliation FAILED, never silently adjusted")


@dataclass(frozen=True)
class Waterfall:
    credits: Decimal
    stop_costs: Decimal
    recoveries: Decimal
    buybacks: Decimal
    fees: Decimal
    slippage: Decimal
    net: Decimal
    premium_capture: Decimal | None  # net / credits, exact fraction; None if no credit


def build_waterfall(*, credits: Decimal, stop_costs: Decimal, recoveries: Decimal,
                     buybacks: Decimal, fees: Decimal, slippage: Decimal,
                     expected_net: Decimal | None = None) -> Waterfall:
    """Build the waterfall and reconcile it. If `expected_net` is supplied
    (typically the period's independently-folded realized P&L) and disagrees
    with the components' own arithmetic net, raise `WaterfallResidualError`
    rather than returning a bar set that silently doesn't add up."""
    net = credits - stop_costs + recoveries - buybacks - fees - slippage
    if expected_net is not None and net != expected_net:
        raise WaterfallResidualError(expected_net, net)
    premium_capture = (net / credits) if credits else None
    return Waterfall(credits=credits, stop_costs=stop_costs, recoveries=recoveries,
                      buybacks=buybacks, fees=fees, slippage=slippage, net=net,
                      premium_capture=premium_capture)
