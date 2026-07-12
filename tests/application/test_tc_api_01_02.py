"""TC-API-01 (EC-API-01/REC-06 session auth) and TC-API-02 (EC-API-02 rate-limit
priority). Auth uses proactive renewal + a backoff/block/alert gate; the 429
priority behaviour reuses the global PriorityRateLimiter."""
import asyncio

from meic.application.rate_limit import PriorityRateLimiter, RateLimited
from meic.application.session_auth import AuthGate
from meic.application.session_health import health_actions


# --- TC-API-01: proactive renewal; auth failure -> backoff, block, alert ------

def test_tc_api_01_proactive_renewal_and_auth_failure_blocks_entries():
    """TC-API-01 (EC-API-01/REC-06): the token is renewed proactively on its
    interval; a forced auth failure backs off (exponential), blocks entries and
    alerts; a later success clears the block. Resting stops are never involved."""
    # proactive renewal fires on the refresh interval, before any failure
    assert "refresh" in health_actions(
        market_open=True, seconds_since_probe=1, seconds_since_refresh=301,
        probe_interval=60, refresh_interval=300)

    gate = AuthGate(base_backoff_seconds=1.0)
    assert gate.entries_blocked is False

    r1 = gate.on_auth_failure()
    assert r1["block_entries"] is True and gate.entries_blocked is True
    assert r1["backoff_seconds"] == 1.0
    assert r1["alert"] == ("critical", "session_auth_failure")

    r2 = gate.on_auth_failure()
    assert r2["backoff_seconds"] == 2.0          # exponential backoff
    r3 = gate.on_auth_failure()
    assert r3["backoff_seconds"] == 4.0

    gate.on_auth_success()                        # renewal succeeded
    assert gate.entries_blocked is False and gate.failures == 0

    # structural: the gate governs entries only — no stop/protection control on it
    assert not any(hasattr(gate, a) for a in ("cancel_stop", "flatten", "protect"))


# --- TC-API-02: exit-side always sent before entries under 429s, none dropped -

def test_tc_api_02_exit_side_sent_before_entries_none_dropped():
    """TC-API-02 (EC-API-02): under injected 429s, exit-side requests are always
    dispatched before queued entry-side requests and none is dropped."""
    limiter = PriorityRateLimiter(max_entry_retries=3)

    def flaky(n_429s: int, label: str):
        state = {"n": 0}
        async def fn():
            if state["n"] < n_429s:
                state["n"] += 1
                raise RateLimited(label)
            return label
        return fn

    # entries are queued FIRST, but exits must still go out first; every request
    # is rate-limited a few times before it lands.
    requests = [
        (False, flaky(1, "entry-1"), "entry-1"),
        (False, flaky(1, "entry-2"), "entry-2"),
        (True, flaky(2, "exit-stop"), "exit-stop"),
        (True, flaky(2, "exit-flatten"), "exit-flatten"),
    ]
    results = asyncio.run(limiter.run(requests))

    # exits dispatched before either entry; nothing dropped
    assert limiter.dispatched[:2] == ["exit-stop", "exit-flatten"]
    assert set(limiter.dispatched) == {"exit-stop", "exit-flatten", "entry-1", "entry-2"}
    assert limiter.dropped == []
    assert results == ["entry-1", "entry-2", "exit-stop", "exit-flatten"]
