"""TPF-03d — an armed exit that cannot be evaluated is SURFACED, never silent.

THE NFR-09 SHAPE, stated in the rule itself: an unmarkable leg makes the cost
function return None, which reads as "no breach" — INDISTINGUISHABLE from "not
breached" to every consumer downstream. So the operator believes a floor is
protecting them while it has not been evaluated for hours. Nothing is broken,
nothing errors, and nothing fires.

That is the same failure as the 60 s blind window, only quieter: there, exits
were evaluated too rarely; here they are not evaluated at all, and the system
looks identical either way. "Silence is indistinguishable from safety."

TPF-03b(iii) is tracked here too, because it produces exactly the same
observable: a `now_ms` that does not ADVANCE between passes cannot accumulate
a continuous breach, so a floor can never confirm. A required argument catches
an ABSENT clock; nothing catches a STOPPED one — except noticing that time
isn't moving while exits are armed.

CONTINUOUSLY is the operative word. A leg that flickers in and out of
markability is not the condition this alerts on; recovery CLEARS the elapsed
time, the same discipline TPF-03b uses for breach confirmation, and for the
same reason: an accumulator would eventually alert on a leg that was fine
almost all the time.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Unevaluable:
    """One entry's unevaluable episode, as the card and the alert need it."""

    entry_id: str
    reason: str
    since_s: float
    seconds: float


class ExitEvaluabilityTracker:
    """Per-entry "how long has this armed exit been unevaluable?".

    Time is passed IN (`now_s`), the same discipline as the TPF-03b monitors:
    no clock dependency, exact tests, one reading per pass.
    """

    def __init__(self, *, alert_after_s: float) -> None:
        self._alert_after_s = max(0.0, float(alert_after_s))
        self._since: dict[str, tuple[float, str]] = {}
        self._alerted: set[str] = set()

    def observe(self, entry_id: str, *, evaluable: bool, reason: str = "",
                now_s: float) -> Unevaluable | None:
        """Record this pass's outcome for one armed entry.

        Returns an `Unevaluable` exactly ONCE per episode, at the moment the
        condition has persisted for `alert_after_s`. Once per EPISODE, not
        once per pass: at 250 ms a per-pass return would be four alerts a
        second, and the caller's rate limiter should be a backstop rather than
        the only thing standing between the operator and a flood."""
        if evaluable:
            self.recovered(entry_id)
            return None

        started = self._since.get(entry_id)
        if started is None:
            self._since[entry_id] = (now_s, reason)
            return None

        since_s, first_reason = started
        elapsed = now_s - since_s
        if elapsed < self._alert_after_s or entry_id in self._alerted:
            return None

        self._alerted.add(entry_id)
        return Unevaluable(entry_id=entry_id, reason=first_reason or reason,
                           since_s=since_s, seconds=elapsed)

    def recovered(self, entry_id: str) -> None:
        """The entry became evaluable again -- CLEAR, never pause, so a
        flickering leg cannot accumulate its way to an alert. Clearing
        `_alerted` too means a genuine RECURRENCE alerts again: a floor that
        goes blind, recovers, and goes blind once more is two incidents."""
        self._since.pop(entry_id, None)
        self._alerted.discard(entry_id)

    def state_for(self, entry_id: str, *, now_s: float) -> Unevaluable | None:
        """The card's view: unevaluable RIGHT NOW, and for how long.

        Distinct from `observe` on purpose -- the card must show the condition
        from the FIRST pass that sees it, while the ALERT waits for the
        threshold. Showing the operator a degraded exit immediately, and only
        interrupting them once it persists, is the difference between a useful
        indicator and a noisy one."""
        started = self._since.get(entry_id)
        if started is None:
            return None
        since_s, reason = started
        return Unevaluable(entry_id=entry_id, reason=reason, since_s=since_s,
                           seconds=now_s - since_s)

    def forget(self, entry_id: str) -> None:
        """The entry reached a terminal status -- drop it entirely, so a
        reused id never inherits a stale episode."""
        self.recovered(entry_id)


class ClockStallDetector:
    """TPF-03b(iii): is the evaluation clock actually ADVANCING?

    A `now_ms` that repeats cannot accumulate a continuous breach, so every
    armed floor silently stops being able to confirm while looking perfectly
    healthy. `now_ms` being REQUIRED catches an absent clock; only this
    catches a stopped one."""

    def __init__(self) -> None:
        self._last_ms: int | None = None

    def advanced(self, now_ms: int) -> bool:
        """True if the clock moved since the previous pass. The FIRST pass
        counts as advanced -- there is no prior reading to compare, and
        treating "no evidence yet" as a stall would alert on every boot."""
        last, self._last_ms = self._last_ms, now_ms
        return last is None or now_ms > last
