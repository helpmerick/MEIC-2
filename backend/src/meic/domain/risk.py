"""Risk gates — RSK-04 (max exposure), RSK-05 (fat-finger / quote sanity),
RSK-08 (daily order cap). Pure; the application RiskGate wraps every
order-submitting path with these.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


def worst_case_loss(
    width: Decimal, net_credit: Decimal, *, contracts: int = 1,
    multiplier: Decimal = Decimal("100"),
) -> Decimal:
    """RSK-04: per-condor worst case = (width − credit) × multiplier × contracts —
    only ONE side can settle in the money, per side not both.

    UND-02 (v1.86): `multiplier` is the traded underlying's PROFILE multiplier
    (`domain.underlying.UnderlyingProfile.multiplier`) — SPX/RUT are ×100,
    /ES is ×50. Default 100 keeps every pre-v1.86 caller byte-identical.
    """
    return max(Decimal("0"), width - net_credit) * multiplier * contracts


def day_worst_case(
    entries: list[tuple[Decimal, Decimal, int]] | list[tuple[Decimal, Decimal, int, Decimal]],
    *, multiplier: Decimal = Decimal("100"),
) -> Decimal:
    """RSK-04 (v1.44): contracts are PER ENTRY, so the day's exposure is the SUM
    of each entry's own worst case — `2 × wc₁ + 1 × wc₂`, NEVER `3 × max(wc)`.

    entries: (wing_width, net_credit, contracts) per open/proposed entry, OR
    (wing_width, net_credit, contracts, multiplier) when an entry's own
    multiplier differs from the day-wide default (UND-02, v1.86: "a mixed-
    underlying day sums each entry's worst case at its own multiplier" —
    e.g. an SPX entry ×100 alongside an /ES entry ×50 in the SAME sum,
    NEVER a shared multiplier). A 3-tuple entry uses the `multiplier`
    keyword (default 100) — byte-identical to every pre-v1.86 caller.
    """
    total = Decimal("0")
    for entry in entries:
        if len(entry) == 4:
            w, c, n, m = entry
        else:
            w, c, n = entry
            m = multiplier
        total += worst_case_loss(w, c, contracts=n, multiplier=m)
    return total


def exceeds_max_day_risk(open_worst_cases: list[Decimal], new_worst_case: Decimal, max_day_risk: Decimal) -> bool:
    """RSK-04: block a new entry when Σ(open worst cases) + its own exceeds the cap."""
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
