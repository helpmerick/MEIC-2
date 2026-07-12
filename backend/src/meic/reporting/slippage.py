"""RPT-06 slippage-in / RPT-07 slippage-out, four families kept separate.

`slippage_in` is POSITIVE for price improvement (doc 10: "v1.52's 3.50->3.60
is +0.10 and must display as such" -- never sign-flipped into a loss).
`stop_slippage` is EC-STP-03's "fill - trigger", in dollars and ticks. The
mean/p50/p90/max aggregates below serve every one of RPT-07's four families
(stop-outs, long recovery, closes, decay buybacks) identically -- they take a
plain list of Decimal samples, whatever the caller's family is.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal


def slippage_in(first_rung_credit: Decimal, fill_credit: Decimal) -> Decimal:
    """RPT-06: fill credit minus first-rung credit. Positive = price
    improvement (the seller collected MORE than the first rung offered)."""
    return fill_credit - first_rung_credit


def stop_slippage(trigger: Decimal, fill: Decimal, *, tick: Decimal = Decimal("0.05")
                   ) -> tuple[Decimal, Decimal]:
    """EC-STP-03: (dollars, ticks) of gap-through-the-stop slippage."""
    dollars = fill - trigger
    return dollars, dollars / tick


def mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / len(values)


def _nearest_rank_percentile(values: list[Decimal], pct: Decimal) -> Decimal | None:
    """Nearest-rank percentile over a sorted copy -- deterministic, no
    interpolation (consistent with this module's no-fabrication stance)."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    raw_rank = (pct / Decimal(100)) * Decimal(n)
    rank = int(raw_rank.to_integral_value(rounding=ROUND_CEILING))
    idx = max(0, min(n, rank) - 1)
    return ordered[idx]


def p50(values: list[Decimal]) -> Decimal | None:
    return _nearest_rank_percentile(values, Decimal("50"))


def p90(values: list[Decimal]) -> Decimal | None:
    return _nearest_rank_percentile(values, Decimal("90"))


def maximum(values: list[Decimal]) -> Decimal | None:
    return max(values) if values else None
