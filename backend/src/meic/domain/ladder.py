"""Reprice ladder — ORD-02/03 (entry) and LEX-03/04 (long exit) MECHANICS only.

A ladder is a pure price sequence: start at a price, step one tick down per
attempt, never below the floor. Timing (entry_reprice_seconds,
lex_reprice_seconds), order I/O, cancel/replace, and the LEX-05 marketable
fallback are application-layer. Nothing here places, cancels, or times
anything.

Floors differ by use and are the CALLER's policy:
  entry (ORD-03): min_total_credit — reaching the floor unfilled means
                  cancel-and-skip (application decides)
  LEX (LEX-04):   max(current bid, intrinsic) — never sell below either
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .ticks import TickTable


@dataclass(frozen=True)
class LadderStep:
    attempt: int  # 0 = initial price, 1..n = reprices
    price: Decimal


@dataclass(frozen=True)
class RepriceLadder:
    """start: entry = net-credit mid (ORD-02); LEX = long's mid (LEX-03).
    attempts: reprices AFTER the initial price (ORD-02 default 5, LEX-03 default 4).
    """

    start: Decimal
    ticks: TickTable
    attempts: int
    floor: Decimal | None = None

    def prices(self) -> tuple[LadderStep, ...]:
        """The full legal price sequence. Shorter than attempts+1 when the
        floor cuts it off — an exhausted ladder is the caller's signal to
        cancel/skip (ORD-03) or go to the LEX-05 fallback."""
        first = self.ticks.round(self.start)
        if self.floor is not None and first < self.floor:
            return ()
        steps = [LadderStep(0, first)]
        price = first
        for attempt in range(1, self.attempts + 1):
            price = price - self.ticks.tick_for(price)
            if self.floor is not None and price < self.floor:
                break
            steps.append(LadderStep(attempt, price))
        return tuple(steps)


def intrinsic_put(strike: Decimal, spot: Decimal) -> Decimal:
    """LEX-04: intrinsic = max(0, strike - spot) for puts."""
    return max(Decimal(0), strike - spot)


def intrinsic_call(strike: Decimal, spot: Decimal) -> Decimal:
    """LEX-04: intrinsic = max(0, spot - strike) for calls."""
    return max(Decimal(0), spot - strike)


def lex_floor(bid: Decimal, intrinsic: Decimal) -> Decimal:
    """LEX-04: never place a sell below max(current bid, intrinsic value)."""
    return max(bid, intrinsic)
