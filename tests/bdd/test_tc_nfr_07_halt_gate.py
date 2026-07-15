"""Step definitions for TC-NFR-07's third scenario -- "The halt gate blocks
when unmeasured (DAT-04a)" (v1.69, operator-ratified — NFR-07's ninth
finding, fixed).

Pure/offline: exercises `TradingStatusStore` + `LiveMarketGates` +
`evaluate_gates` directly -- the same level `tests/application/test_clocks.py`
and `tests/application/test_live_selection.py` already test `LiveMarketGates`
at. The REAL `live_app()` behavioral proof (the piggybacked DXLink Profile
subscription actually wired end to end) is
`tests/bdd/test_tc_nfr_07_constant_signal.py`'s halted step; this file is the
DAT-04a decision-table itself: no reading, a stale reading, and a non-ACTIVE
status all block identically, and all four ENT-03 gate inputs this module
sources share the "unmeasured = blocked" contract.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from pytest_bdd import given, scenario, then

from meic.adapters.dxlink.trading_status import (
    HALT_READING_STALE_AFTER_SECONDS,
    TradingStatusStore,
)
from meic.application.entry_gates import evaluate_gates
from meic.composition.live_gates import LiveMarketGates

# A Wednesday, well inside RTH (10:00 ET == 14:00 UTC in July/EDT) — the
# calendar's own market_open must be True so `halted` is actually EVALUATED
# rather than short-circuited True by `if open_now else True`.
NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


class _FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


async def _async(value):
    return value


def _gates_snapshot(**kw):
    defaults = dict(data_fresh=lambda: _async(True), session_valid=lambda: _async(True),
                     buying_power_ok=lambda: _async(True))
    defaults.update(kw)
    return asyncio.run(LiveMarketGates(clock=_FixedClock(NOW), **defaults)())


@scenario("../features/TC-NFR-07.feature", "The halt gate blocks when unmeasured (DAT-04a)")
def test_halt_gate_blocks_when_unmeasured():
    pass


@given("no trading-status reading, or one stale beyond 300 seconds", target_fixture="world")
def _given_no_or_stale_reading():
    return {"store": TradingStatusStore()}


@then('entries are blocked with reason "market_halted"')
def _then_blocked_reason_market_halted(world):
    store: TradingStatusStore = world["store"]

    # (a) no reading at all -- unmeasured = unverified = blocked (RSK-07).
    assert store.halted(NOW) is True
    snap = _gates_snapshot(halted=lambda: store.halted(NOW))
    assert snap.market_halted is True
    assert evaluate_gates(snap) == "market_halted"

    # (b) a reading exists but is stale beyond the 300 s bound.
    store.record("ACTIVE", NOW - timedelta(seconds=HALT_READING_STALE_AFTER_SECONDS + 1))
    assert store.halted(NOW) is True
    snap_stale = _gates_snapshot(halted=lambda: store.halted(NOW))
    assert snap_stale.market_halted is True
    assert evaluate_gates(snap_stale) == "market_halted"


@then("a status of not-active blocks identically")
def _then_not_active_blocks_identically(world):
    store: TradingStatusStore = world["store"]
    for status in ("HALTED", "UNDEFINED", "INACTIVE"):
        store.record(status, NOW)
        assert store.halted(NOW) is True, f"status={status!r} must block identically"
        snap = _gates_snapshot(halted=lambda: store.halted(NOW))
        assert snap.market_halted is True
        assert evaluate_gates(snap) == "market_halted"

    # ...and the ONE non-blocking case: ACTIVE + fresh.
    store.record("ACTIVE", NOW)
    assert store.halted(NOW) is False
    snap_ok = _gates_snapshot(halted=lambda: store.halted(NOW))
    assert snap_ok.market_halted is False
    assert evaluate_gates(snap_ok) is None


@then("all four gate inputs share False-means-block polarity")
def _then_all_four_inputs_share_polarity(world):
    # data_fresh / session_valid / buying_power_ok: an absent (None) provider
    # already yields the blocking (False) answer -- untouched by this change,
    # pinned here so the "all four" claim is checked, not assumed.
    for missing in ("data_fresh", "session_valid", "buying_power_ok"):
        kw = dict(data_fresh=lambda: _async(True), session_valid=lambda: _async(True),
                  buying_power_ok=lambda: _async(True), halted=lambda: _async(False))
        kw[missing] = None
        snap = _gates_snapshot(**kw)
        assert getattr(snap, missing) is False, f"{missing}=None must yield the blocking answer"

    # halted (DAT-04a, the ninth finding's fix): an absent provider now
    # yields market_halted=True (blocked) -- the ONE input that used to be
    # inverted (default=False, permissive) is now uniform with the above three.
    snap = _gates_snapshot(halted=None)
    assert snap.market_halted is True, "an absent halted provider must block (DAT-04a)"
    assert evaluate_gates(snap) == "market_halted"
