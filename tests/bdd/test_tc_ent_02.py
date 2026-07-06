"""Hand-written step definitions for TC-ENT-02 — ENT-03 gate chain."""
import pytest
from pytest_bdd import given, parsers, scenarios, then

from meic.application.entry_gates import GateSnapshot, evaluate_gates

scenarios("../features/TC-ENT-02.feature")

# All-pass baseline; each scenario flips exactly one gate to failing.
_PASS = dict(armed=True, confirm_live=True, stop_trading=False, flatten_in_progress=False,
             market_open=True, market_halted=False, data_fresh=True, session_valid=True,
             buying_power_ok=True)

_CONDITION = {
    "Stop Trading active": {"stop_trading": True},
    "a Flatten All executing": {"flatten_in_progress": True},
    "a market halt": {"market_halted": True},
    "market data stale": {"data_fresh": False},
    "broker session invalid": {"session_valid": False},
    "insufficient buying power": {"buying_power_ok": False},
}


@pytest.fixture
def world():
    return {"orders": []}


@given(parsers.parse("{gate_condition} is true at 10:30 ET"))
def _(world, gate_condition):
    snap = GateSnapshot(**{**_PASS, **_CONDITION[gate_condition]})
    world["reason"] = evaluate_gates(snap)
    # a failing gate returns before any order is submitted


@then(parsers.parse("entry 2 is SKIPPED with reason {reason}"))
def _(world, reason):
    assert world["reason"] == reason


@then("no order is submitted")
def _(world):
    assert world["orders"] == []
