"""Session health loop — NFR-02 (pure decision).

During market hours: probe the session every session_probe_seconds and
proactively refresh every session_refresh_seconds; a probe hitting a token
error triggers an immediate refresh. Day-scoped: nothing probes outside market
hours. This keeps the account stream (stop-fill events → LEX) alive all day.
"""
from __future__ import annotations


def health_actions(
    *,
    market_open: bool,
    seconds_since_probe: float,
    seconds_since_refresh: float,
    probe_interval: float,
    refresh_interval: float,
    probe_error: bool = False,
) -> set[str]:
    """Return the set of due actions: {} | {"probe"} | {"refresh"} | both.
    Outside market hours the set is ALWAYS empty (zero probes)."""
    if not market_open:
        return set()
    actions: set[str] = set()
    if probe_error:
        actions.add("refresh")  # a token error forces an immediate refresh
    if seconds_since_probe >= probe_interval:
        actions.add("probe")
    if seconds_since_refresh >= refresh_interval:
        actions.add("refresh")
    return actions
