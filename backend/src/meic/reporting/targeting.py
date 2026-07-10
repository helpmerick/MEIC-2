"""RPT-05 targeting quality -- per side per entry, decomposed by cause.

Pure arithmetic over values the caller already has: `selection_gap` from the
STK-11 probe-walk log (the matched probe premium vs the entry's target),
`execution_gap` from the ORD-09 fill record (the short's actual fill vs the
mid it was selected at), and `wing_drag` from the same fill record (gross
short premium vs the net credit actually collected on that side, i.e. the
long's cost). This module has no fold/event dependency of its own -- it is
the same arithmetic regardless of whether the caller is live code computing
these at selection/fill time or the reporting layer reading them back from
recorded logs.
"""
from __future__ import annotations

from decimal import Decimal


def selection_gap(matched_probe: Decimal, target: Decimal) -> Decimal:
    """Matched probe premium minus the entry's target premium (STK-02's probe
    walk bounds this to +0.15/-1.25 via probe_up_max/probe_down_max -- that
    bound is enforced upstream in domain/walk.py; this is plain subtraction)."""
    return matched_probe - target


def execution_gap(short_fill: Decimal, selected_mid: Decimal) -> Decimal:
    """The short leg's actual fill price minus the mid it was selected at."""
    return short_fill - selected_mid


def wing_drag(gross_short: Decimal, net_credit_per_side: Decimal) -> Decimal:
    """Gross short premium minus the net credit actually collected on that
    side -- the long wing's cost."""
    return gross_short - net_credit_per_side
