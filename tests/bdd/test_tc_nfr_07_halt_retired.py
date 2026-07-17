"""Step definitions for TC-NFR-07's THIRD scenario -- "The halt input is
retired, never stubbed (DAT-04a v1.80 contingency executed)".

This REPLACES `tests/bdd/test_tc_nfr_07_halt_gate.py` (deleted), which bound
the v1.69 scenario "The halt gate blocks when unmeasured (DAT-04a)" -- that
scenario no longer exists in the spec/generated feature file, because DAT-04a
v1.80 (operator-ruled, market-taught) executed its own pre-ruled contingency:
live use proved the underlying's dxfeed Profile `trading_status` reads
UNDEFINED in real trading windows, so the field is unusable, and a submitted
patch treating UNDEFINED as tradeable was REJECTED (fail-open on a
broker-unverifiable state; a permanently-UNDEFINED field is itself the NFR-07
can-never-say-no constant-signal defect). The dedicated halt input is RETIRED
outright: module deleted, gate input removed (never stubbed to a constant),
`market_halted` skip reason retired. Halt protection is now formally carried
by the freshness gates (DAT-02/STK-04/STK-10); DAY-01/DAY-06 keep gating
market hours.
"""
from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import fields
from datetime import datetime, timezone

from pytest_bdd import given, scenario, then

from meic.application.entry_gates import evaluate_gates
from meic.composition.live_gates import LiveMarketGates
from meic.composition.wiring_registry import SAFETY_GATE_REGISTRY, unexpectedly_not_live


class _FixedClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


NOW = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)   # Thursday, well inside RTH


async def _async(value):
    return value


def _gates_snapshot(**kw):
    defaults = dict(data_fresh=lambda: _async(True), session_valid=lambda: _async(True),
                     buying_power_ok=lambda: _async(True))
    defaults.update(kw)
    return asyncio.run(LiveMarketGates(clock=_FixedClock(NOW), **defaults)())


@scenario("../features/TC-NFR-07.feature",
          "The halt input is retired, never stubbed (DAT-04a v1.80 contingency executed)")
def test_halt_input_retired_scenario():
    pass


@given("the retired trading-status input", target_fixture="world")
def _given_the_retired_input():
    return {}


@then("no halt module, gate input, or market_halted skip reason exists in the build")
def _then_no_halt_module_gate_or_reason(world):
    # (1) the module itself is deleted, not merely unwired.
    assert importlib.util.find_spec("meic.adapters.dxlink.trading_status") is None, (
        "meic.adapters.dxlink.trading_status must be DELETED (DAT-04a v1.80 contingency)")

    # (2) LiveMarketGates carries no `halted` field/parameter at all.
    field_names = {f.name for f in fields(LiveMarketGates)}
    assert "halted" not in field_names, "LiveMarketGates must not carry a `halted` field"

    # (3) the wiring registry's SAFETY_GATE_REGISTRY has no `halted` entry.
    assert not any(e.gate_input == "halted" for e in SAFETY_GATE_REGISTRY), (
        "no SAFETY_GATE_REGISTRY entry may reference `halted` -- it is retired")

    # (4) a real LiveMarketGates evaluation, market open, never produces
    # market_halted=True -- there is no live producer of that reason left.
    snap = _gates_snapshot()
    assert snap.market_open is True
    assert snap.market_halted is False
    assert evaluate_gates(snap) is None


@then("no gate input was replaced by a constant (the wiring audit still fails constants)")
def _then_audit_still_bites_a_constant_on_remaining_inputs(world):
    """The retirement of ONE input must not have quietly weakened the
    constant-signal audit for the inputs that remain. Reuse the exact
    fail-first shape `test_tc_nfr_07_constant_signal.py` uses for
    `flatten_in_progress`: rebind it to `lambda: False` and prove the audit
    still flags it -- this scenario proves the audit as a WHOLE is not
    broken by the removal, without re-booting a full live_app() here."""
    from types import SimpleNamespace

    from meic.composition.wiring_registry import _flatten_in_progress_live_check

    commands = SimpleNamespace(_flatten_in_progress=False)
    gates = SimpleNamespace(flatten_in_progress=lambda: False)   # THE constant regression shape
    runtime = SimpleNamespace(market_gates=gates)
    state = SimpleNamespace(runtime=runtime, commands=commands)

    result = _flatten_in_progress_live_check(state)
    assert result.live is False, "a constant flatten_in_progress provider must still fail the audit"


@then("a frozen-quote scenario still blocks entries via the freshness gates with their own reasons")
def _then_frozen_quote_blocks_via_freshness_not_halt(world):
    # A stale/frozen chain snapshot -> data_fresh=False -> DAT-02's own
    # reason, "data_unavailable" -- never "market_halted" (that producer no
    # longer exists at all, per the step above).
    snap = _gates_snapshot(data_fresh=lambda: _async(False))
    assert snap.market_halted is False
    assert evaluate_gates(snap) == "data_unavailable"
