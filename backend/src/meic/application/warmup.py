"""Entry warm-up — ENT-08 / REC-06 (pure decision).

At T-`warmup_lead_seconds` before each scheduled entry the bot warms up: it
proactively renews a near-expiry session token, resubscribes any silently stale
market-data stream, and primes the chain — so the entry fires on time with a
valid session and fresh quotes. The warm-up NEVER delays the entry: if it
cannot restore the session it alerts and the entry is SKIPPED `invalid_session`
at its scheduled time (the ENT-03 gate decides), the clock never slips.
"""
from __future__ import annotations

from dataclasses import dataclass

WARMUP_LEAD_SECONDS = 60      # T-60: warm-up begins
RENEW_BY_SECONDS = 30         # renewal must complete before T-30
ALERT_AT_SECONDS = 10         # unrecoverable session alerts by T-10


@dataclass(frozen=True)
class WarmupResult:
    renewed: bool
    resubscribed: bool
    session_ok: bool
    alert: tuple[str, str] | None
    entry_reason: str | None   # None = entry fires; else the ENT-03 skip reason
    entry_delayed: bool        # ENT-08: ALWAYS False — the clock never slips


def plan_warmup(
    *,
    token_expires_in_seconds: float,
    stream_stale: bool = False,
    renewal_succeeds: bool = True,
    near_expiry_seconds: float = 300,
) -> WarmupResult:
    """Decide what the warm-up does. A token expiring within near_expiry_seconds
    is renewed proactively (REC-06); a stale stream is resubscribed (STK-04); a
    renewal that keeps failing leaves the session invalid — alert + skip, never
    delay."""
    renewed = False
    session_ok = True
    alert = None

    if token_expires_in_seconds <= near_expiry_seconds:
        if renewal_succeeds:
            renewed = True
        else:
            session_ok = False
            alert = ("critical", "session_unrecoverable")

    resubscribed = bool(stream_stale)
    entry_reason = None if session_ok else "invalid_session"
    return WarmupResult(renewed=renewed, resubscribed=resubscribed, session_ok=session_ok,
                        alert=alert, entry_reason=entry_reason, entry_delayed=False)


def warmup_runs(*, recovery_done_seconds_before: float,
                lead_seconds: float = WARMUP_LEAD_SECONDS) -> bool:
    """ENT-08: if the bot boots INSIDE the warm-up window (recovery finishes at
    less than lead_seconds before the entry), the warm-up still runs — compressed
    — rather than being skipped. It only skips when there is no time at all."""
    return recovery_done_seconds_before > 0
