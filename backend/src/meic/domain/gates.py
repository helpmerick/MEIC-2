"""Credit gates — STK-05 (per-short gross floor, v1.29) and STK-06 (total NET floor).

The two bases differ BY DESIGN (STK-02a): STK-05 floors each SHORT leg's
gross premium (wings NOT factored); STK-06 floors the TOTAL net credit with
longs factored. A thin-net side trades when the total floor passes — the
per-side NET floor deliberately does not exist (TC-STK-02).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class GatesPassed:
    total_net_credit: Decimal


@dataclass(frozen=True)
class GatesFailed:
    reason: str  # "insufficient_credit"


def check_credit_gates(
    *,
    put_short_mid: Decimal,
    call_short_mid: Decimal,
    total_net_credit_mid: Decimal,
    min_short_premium: Decimal,
    min_total_credit: Decimal,
) -> GatesPassed | GatesFailed:
    """Single-side entries are prohibited (STK-05): either short failing its
    gross floor skips the ENTIRE entry, as does a total net below STK-06."""
    if put_short_mid < min_short_premium or call_short_mid < min_short_premium:
        return GatesFailed("insufficient_credit")
    if total_net_credit_mid < min_total_credit:
        return GatesFailed("insufficient_credit")
    return GatesPassed(total_net_credit_mid)
