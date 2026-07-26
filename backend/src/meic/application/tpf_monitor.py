"""TPFMonitor — bot-side take-profit floor monitoring (TPF-03/09).

The floor is explicitly NOT broker-resting: the bot marks profit and, when it
has sat at or below the armed floor CONTINUOUSLY for `tp_confirmation_ms`,
routes the close through CloseEntry(take_profit) (TPF-04, one close path —
CLS-02). A single bad print does not fire; a recovery or a stale mark CLEARS
the elapsed time (EC-TPF-02).

TPF-03b (v1.94) — CONFIRMATION IS A DURATION, NEVER A COUNT. This class used
to confirm after `tp_confirmation_evals` consecutive valid evaluations, and
that count SILENTLY RE-DENOMINATED ITSELF the moment the evaluation cadence
changed: "2" meant two adjacent prints on a 60 s tick — two MINUTES — and
becomes 500 ms at the 250 ms cadence, with no edit to any config file.
**Nobody changed the parameter; the ground moved under it.**

TPF-03b(ii) — THE COINCIDENCE IS THE TRAP. 2 evals x 250 ms = 500 ms, which
is EXACTLY the ratified `tp_confirmation_ms` default. On DEFAULT config the
accidental re-denomination lands precisely on the intended value, so every
test passes and nothing looks wrong — while an operator who TUNED the count
is silently wrong by the full cadence ratio (a 5, meaning five minutes,
becomes 1.25 s). A silent re-denomination that MATCHES the default is more
dangerous than one that does not, because it survives casual testing. The
migration is therefore verified against a NON-DEFAULT value, never the
default.

A duration cannot be silently reinterpreted, which is the whole point.

Profit is bot-computed, deterministic (PNL-03); the domain math (armable
levels, floor amount) lives in domain/tpf.py — this service owns the trigger
loop and the elapsed-breach clock.
"""
from __future__ import annotations

from decimal import Decimal

from meic.domain.tpf import breached


class TPFMonitor:
    """Fires when the floor has been breached CONTINUOUSLY for long enough.

    Time is passed IN (`now_ms`) rather than read from a clock here: the
    monitor stays free of I/O, every duration test is exact rather than
    dependent on real elapsed time, and the caller's single reading of "now"
    is shared by every entry in the pass so two entries cannot disagree about
    when the pass happened.
    """

    def __init__(self, *, tp_confirmation_ms: int = 500) -> None:
        self._confirmation_ms = max(0, int(tp_confirmation_ms))
        # WHEN the current continuous breach began, or None when not breached.
        # A START TIMESTAMP, never an accumulator: TPF-03b requires a recovery
        # or an invalid evaluation to CLEAR the elapsed time rather than pause
        # it, and an accumulator would let a flickering mark bank progress
        # across recoveries and fire on a breach that was never continuous.
        self._breach_started_ms: int | None = None

    @property
    def breach_started_ms(self) -> int | None:
        """When the current continuous breach began — for TPF-03d surfacing
        and diagnostics only, never a trigger input."""
        return self._breach_started_ms

    def evaluate(self, *, profit: Decimal, floor: Decimal, stale: bool = False,
                 now_ms: int) -> bool:
        """Return True exactly when the close should fire this evaluation.

        stale ⇒ CLEAR (EC-TPF-02): an invalid evaluation is not evidence that
        the breach continued, so it cannot count toward a CONTINUOUS breach
        and must not merely pause the timer. Recovery above the floor ⇒ CLEAR.
        """
        if stale:
            self._breach_started_ms = None
            return False
        if not breached(floor, profit):
            self._breach_started_ms = None   # recovery breaks continuity
            return False

        if self._breach_started_ms is None:
            self._breach_started_ms = now_ms
        # `tp_confirmation_ms = 0` fires on the FIRST valid breach: the elapsed
        # span is 0 and 0 >= 0 (TPF-03b, explicit).
        if now_ms - self._breach_started_ms >= self._confirmation_ms:
            self._breach_started_ms = None   # fired — start clean if re-armed
            return True
        return False
