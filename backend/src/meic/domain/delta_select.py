"""Delta strike selection — STK-02 `delta` method.

Choose the strike whose absolute delta is closest to short_delta_target
without exceeding short_delta_max. The boundary strike at exactly the max is
eligible.
"""
from __future__ import annotations

from decimal import Decimal


def select_by_delta(
    strikes_deltas: list[tuple[Decimal, Decimal]],  # (strike, |delta|)
    *,
    target: Decimal = Decimal("0.10"),
    max_delta: Decimal = Decimal("0.15"),
) -> Decimal | None:
    eligible = [(s, d) for s, d in strikes_deltas if d <= max_delta]  # <= max is eligible
    if not eligible:
        return None
    return min(eligible, key=lambda sd: abs(sd[1] - target))[0]
