"""NFR-08a — exit-evaluation failures ALERT, never log-only.

THE INCIDENT (NFR-08's own): an exit evaluator threw on 15 consecutive health
ticks while EVERY exit was dead, and the session continued. The call site
caught everything and emitted a `logger.warning`, so there was no
operator-visible signal at all — the floors looked armed and were not.

At the ratified 250 ms cadence a silent throw is 240x more frequent and no
more visible, which is why "it is logged" stopped being an acceptable answer.

RATE LIMITING IS PART OF THE RULE, not a convenience. A throw at 250 ms is
four alerts a second; an alert channel emitting four a second is one the
operator mutes, and a muted channel is indistinguishable from silence — the
same failure NFR-08a exists to end, arrived at from the opposite direction.
So: at most one alert per `exit_unevaluable_alert_s` PER DISTINCT ERROR.

"Per distinct error" matters. Keying on the error alone would let a NEW,
different failure be swallowed by an older one's cooldown; keying on nothing
would flood. Two different exceptions are two different facts and each earns
its own first alert immediately.
"""
from __future__ import annotations


class ExitAlertRateLimiter:
    """One alert per key per window. Time is passed IN (`now_s`) — same
    discipline as the TPF-03b monitors: no clock dependency, exact tests, and
    one reading per pass shared by every caller in it."""

    def __init__(self, *, window_s: float) -> None:
        self._window_s = max(0.0, float(window_s))
        self._last_sent: dict[str, float] = {}

    def should_send(self, key: str, *, now_s: float) -> bool:
        """True when this key has not alerted within the window.

        A window of 0 sends every time (no suppression) — the honest reading
        of "no cooldown", never a silent block."""
        last = self._last_sent.get(key)
        if last is not None and (now_s - last) < self._window_s:
            return False
        self._last_sent[key] = now_s
        return True

    def forget(self, key: str) -> None:
        """Drop a key's cooldown so a RECURRENCE after a genuine recovery
        alerts immediately rather than being suppressed by the cooldown left
        over from the previous episode. A fault that comes back is news."""
        self._last_sent.pop(key, None)


def error_key(prefix: str, exc: BaseException) -> str:
    """The rate-limit key for one distinct error.

    Type plus message, not the traceback: two failures of the same kind on the
    same entry are the same fact repeating, while a different exception type
    or a different message is a NEW fact and must not inherit the older one's
    cooldown."""
    return f"{prefix}:{type(exc).__name__}:{exc}"
