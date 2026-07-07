"""Live P&L marking — PNL-03 and the half-day calendar rule (DAY-02), plus the
PNL-04 reconciliation verdict. Pure helpers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal


def conservative_mark(bid: Decimal, ask: Decimal, *, stale: bool) -> Decimal:
    """PNL-03: live marking uses the mid, degrading to the worst of bid/ask when
    stale (conservative — never flatter than reality)."""
    return (bid + ask) / 2 if not stale else min(bid, ask)


def skip_late_on_half_day(entry_time: time, close_time: time, min_minutes_before_close: int) -> bool:
    """DAY-02: on a half day, an entry scheduled at/after
    close − min_time_before_close is skipped."""
    cutoff = (datetime.combine(datetime.today(), close_time) - timedelta(minutes=min_minutes_before_close)).time()
    return entry_time >= cutoff


@dataclass(frozen=True)
class PnlReconcile:
    authoritative: Decimal   # PNL-04: the broker figure wins
    bot_computed: Decimal
    delta: Decimal
    mismatch: bool           # |delta| > tolerance


def reconcile_pnl(bot_pnl: Decimal, broker_pnl: Decimal, *, tolerance: Decimal) -> PnlReconcile:
    """PNL-04: broker transaction history is authoritative; a divergence beyond
    tolerance flags a PnlMismatch (both figures + delta surfaced)."""
    delta = broker_pnl - bot_pnl
    return PnlReconcile(authoritative=broker_pnl, bot_computed=bot_pnl, delta=delta,
                        mismatch=abs(delta) > tolerance)
