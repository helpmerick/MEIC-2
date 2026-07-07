"""Risk gates — RSK-04 (max exposure), RSK-05 (fat-finger / quote sanity),
RSK-08 (daily order cap). Pure; the application RiskGate wraps every
order-submitting path with these.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


def worst_case_loss(width: Decimal, net_credit: Decimal, *, contracts: int = 1) -> Decimal:
    """RSK-04: per-condor worst case = (width − credit) × 100 × contracts —
    only ONE side can settle in the money."""
    return max(Decimal("0"), width - net_credit) * 100 * contracts


def exceeds_max_day_risk(open_worst_cases: list[Decimal], new_worst_case: Decimal, max_day_risk: Decimal) -> bool:
    return sum(open_worst_cases, Decimal("0")) + new_worst_case > max_day_risk


def sane_order_price(price: Decimal, *, reference_mid: Decimal, max_deviation_pct: Decimal) -> bool:
    """RSK-05 fat-finger: reject an order price absurdly far from the mid."""
    if reference_mid <= 0:
        return price >= 0
    return abs(price - reference_mid) <= reference_mid * max_deviation_pct / 100


def sane_quote(bid: Decimal, ask: Decimal) -> bool:
    """RSK-05: reject a crossed or negative inbound quote before any decision."""
    return bid >= 0 and ask >= 0 and bid <= ask


@dataclass
class OrderCap:
    """RSK-08: stay under the daily order cap for new entries; exit-side orders
    (stops, LEX, flatten) are NEVER blocked by the cap."""

    cap: int
    buffer: int = 0
    count: int = 0

    def allow(self, *, exit_priority: bool) -> bool:
        if exit_priority:
            return True                        # risk-reducing orders are never capped
        return self.count < (self.cap - self.buffer)

    def record(self) -> None:
        self.count += 1                        # cancel/replaces count as orders too
