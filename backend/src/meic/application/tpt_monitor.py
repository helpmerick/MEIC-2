"""TPTMonitor — bot-side take-profit target monitoring (TPT-04).

Mirrors `application/tpf_monitor.py` exactly, with the trigger direction
reversed: the target is explicitly NOT broker-resting (TPT-04 — a resting
whole-condor TP limit would rest a second buy order on a short leg that
already carries a resting stop, the exact double-fill race the v1.50
replace-based close exists to prevent). The bot marks profit and, when it has
sat at or above the armed target CONTINUOUSLY for `tp_confirmation_ms`, the
caller routes the close through `CloseEntry(take_profit_target)` (CLS-02) —
this module has no close logic of its own, exactly like TPFMonitor.

TPF-03b (v1.94) applies here IN FULL, by TPT-04's own words: "the SAME
evaluator, on the SAME dedicated owner, under TPF-03a–f in full: one
evaluator, one loop, ONE CONFIRMATION RULE, one freshness rule." So
confirmation here is a DURATION too, and for the same reason — the retired
count re-denominated itself silently when the cadence changed, and its
accidental new value (2 x 250 ms) lands exactly on the 500 ms default, which
is what made it survive casual testing. See TPFMonitor's docstring for the
full rationale; there is deliberately ONE explanation, not two drifting
copies.

TPT-05 (any-stop permanent disarm) is NOT this class's job: it is orchestrated
one level up (see `application/exit_monitor.py`), which is structural (any
`ShortStopped` event on the entry) rather than a timer this monitor tracks.
"""
from __future__ import annotations

from decimal import Decimal

from meic.domain.tpt import reached


class TPTMonitor:
    """Fires when the target has been reached CONTINUOUSLY for long enough.

    Time is passed IN (`now_ms`) — see TPFMonitor for why.
    """

    def __init__(self, *, tp_confirmation_ms: int = 500) -> None:
        self._confirmation_ms = max(0, int(tp_confirmation_ms))
        # A START TIMESTAMP, never an accumulator — see TPFMonitor: an
        # accumulator would let a flickering mark bank progress across
        # pullbacks and fire on a run that was never continuous.
        self._reached_started_ms: int | None = None

    @property
    def reached_started_ms(self) -> int | None:
        """When the current continuous at/above-target run began — for TPF-03d
        surfacing and diagnostics only, never a trigger input."""
        return self._reached_started_ms

    def evaluate(self, *, profit: Decimal, target: Decimal, stale: bool = False,
                 now_ms: int) -> bool:
        """Return True exactly when the close should fire this evaluation.

        stale ⇒ CLEAR (mirrors TPF's EC-TPF-02): an invalid evaluation is not
        evidence that the run continued. A pullback below the target ⇒ CLEAR.
        """
        if stale:
            self._reached_started_ms = None
            return False
        if not reached(target, profit):
            self._reached_started_ms = None   # pullback breaks continuity
            return False

        if self._reached_started_ms is None:
            self._reached_started_ms = now_ms
        # `tp_confirmation_ms = 0` fires on the FIRST valid reach (0 >= 0).
        if now_ms - self._reached_started_ms >= self._confirmation_ms:
            self._reached_started_ms = None   # fired — start clean if re-armed
            return True
        return False
