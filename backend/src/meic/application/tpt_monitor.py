"""TPTMonitor — bot-side take-profit target monitoring (TPT-04).

Mirrors `application/tpf_monitor.py` exactly, with the trigger direction
reversed: the target is explicitly NOT broker-resting (TPT-04 — a resting
whole-condor TP limit would rest a second buy order on a short leg that
already carries a resting stop, the exact double-fill race the v1.50
replace-based close exists to prevent). The bot marks profit and, when it
sits at or above the armed target for `tp_confirmation_evals` consecutive
VALID evaluations, the caller routes the close through
`CloseEntry(take_profit_target)` (CLS-02) — this module has no close logic
of its own, exactly like TPFMonitor.

TPT-05 (any-stop permanent disarm) is NOT this class's job: it is orchestrated
one level up (see `application/exit_monitor.py`), which is structural (any
`ShortStopped` event on the entry) rather than a counter this monitor tracks.
"""
from __future__ import annotations

from decimal import Decimal

from meic.domain.tpt import reached


class TPTMonitor:
    def __init__(self, *, tp_confirmation_evals: int = 2) -> None:
        self._evals = tp_confirmation_evals
        self._count = 0

    def evaluate(self, *, profit: Decimal, target: Decimal, stale: bool = False) -> bool:
        """Return True exactly when the close should fire this tick.

        stale => pause and reset (mirrors TPF's EC-TPF-02): a paused
        evaluation is not a confirmation. At/above target => increment;
        below => reset."""
        if stale:
            self._count = 0
            return False
        if reached(target, profit):
            self._count += 1
            if self._count >= self._evals:
                self._count = 0
                return True
            return False
        self._count = 0  # a single bad print / pullback breaks the streak
        return False
