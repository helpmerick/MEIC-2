"""Session auth failure handling — EC-API-01 / REC-06.

The session token is renewed proactively (see session_health.health_actions).
If an auth call fails anyway, the client backs off with exponential delay,
BLOCKS new entries, and alerts — until a renewal succeeds. Resting stops are
unaffected: they live at the broker, which is the whole point of REC-06, so
this gate governs entries only and never touches protection.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AuthGate:
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    failures: int = 0
    entries_blocked: bool = False

    def on_auth_failure(self) -> dict:
        """Record an auth failure: block entries, compute the next backoff, and
        signal an alert. Returns the response the caller acts on."""
        self.failures += 1
        self.entries_blocked = True
        backoff = min(self.max_backoff_seconds,
                      self.base_backoff_seconds * (2 ** (self.failures - 1)))
        return {
            "block_entries": True,
            "backoff_seconds": backoff,
            "alert": ("critical", "session_auth_failure"),
        }

    def on_auth_success(self) -> None:
        """A renewal (proactive or post-backoff) succeeded — clear the block."""
        self.failures = 0
        self.entries_blocked = False
