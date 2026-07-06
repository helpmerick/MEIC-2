"""Paper-mode fill model — SIM-02 (trade-through) and SIM-03 (stop) — pure.

Deliberately pessimistic (SIM-06): a limit fills only when genuinely executable
against the real market — the spread's NATURAL price satisfies the limit, OR
the net mid beats it by at least sim_fill_through_ticks. Mid merely TOUCHING
the limit never fills (touch-equals-fill flatters paper). Simulated stops fill
at trigger + slippage (SIM-03) — never at the trigger, because real stop-
markets don't.
"""
from __future__ import annotations

from decimal import Decimal


def limit_fills(
    *,
    is_credit: bool,
    limit: Decimal,
    natural: Decimal,
    mid: Decimal,
    tick: Decimal,
    through_ticks: int = 1,
) -> bool:
    """SIM-02: does a limit order fill against the current market?

    natural = the spread priced against you (sell legs at bid, buy legs at ask).
    is_credit True = a net-credit (sell) order: fills if it can be sold for at
    least `limit`. is_credit False = a net-debit (buy) order: fills if it can be
    bought for at most `limit`."""
    through = through_ticks * tick
    if is_credit:
        return natural >= limit or mid >= limit + through
    return natural <= limit or mid <= limit - through


def stop_fill_price(trigger: Decimal, *, tick: Decimal, slippage_ticks: int = 3) -> Decimal:
    """SIM-03: a triggered stop-market never fills at the trigger. Buy-to-close
    stop pays trigger + slippage (worse for the holder)."""
    return trigger + slippage_ticks * tick


def stop_triggered(mark: Decimal, trigger: Decimal) -> bool:
    """SIM-03: the short's mark reaching the trigger fires the stop."""
    return mark >= trigger
