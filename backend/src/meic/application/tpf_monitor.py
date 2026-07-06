"""TPFMonitor — bot-side take-profit floor monitoring (TPF-03/09).

The floor is explicitly NOT broker-resting: the bot marks profit and, when it
sits at or below the armed floor for tp_confirmation_evals consecutive VALID
evaluations, routes the close through CloseEntry(take_profit) (TPF-04, one
close path — CLS-02). A single bad print does not fire; stale marks pause
evaluation and reset the counter (EC-TPF-02).

Profit is bot-computed, deterministic (PNL-03); the domain math (armable
levels, floor amount) lives in domain/tpf.py — this service owns the trigger
loop and the counter.
"""
from __future__ import annotations

from decimal import Decimal

from meic.domain.tpf import breached


class TPFMonitor:
    def __init__(self, *, tp_confirmation_evals: int = 2) -> None:
        self._evals = tp_confirmation_evals
        self._count = 0

    def evaluate(self, *, profit: Decimal, floor: Decimal, stale: bool = False) -> bool:
        """Return True exactly when the close should fire this tick.

        stale ⇒ pause and reset (EC-TPF-02): a paused evaluation is not a
        confirmation. Below-floor ⇒ increment; recovery above ⇒ reset."""
        if stale:
            self._count = 0
            return False
        if breached(floor, profit):
            self._count += 1
            if self._count >= self._evals:
                self._count = 0
                return True
            return False
        self._count = 0  # a single bad print / recovery breaks the streak
        return False
