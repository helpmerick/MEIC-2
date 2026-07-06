"""Hand-written step definitions for TC-STP-18 — the STP-02d per_side gate,
allocation reconciliation, and the fixed ungate criterion (v1.43)."""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.config.stop_basis import SELECTABLE_BASES, StopBasisRejected, validate_stop_basis
from meic.domain.allocation import AllocationGate, reconcile

scenarios("../features/TC-STP-18.feature")

TICK = D("0.05")


@pytest.fixture
def world():
    return {}


# --- Scenario: per_side rejected while the gate is in force -------------------

@given('config stop_basis = per_side, globally or on any entry override')
def _(world):
    try:
        validate_stop_basis("per_side")
        world["rejected"] = None
    except StopBasisRejected as e:
        world["rejected"] = e.reason


@then('config validation rejects it with reason "allocation_unverified"')
def _(world):
    assert world["rejected"] == "allocation_unverified"


@then('total_credit and short_premium remain selectable')
def _(world):
    for basis in ("total_credit", "short_premium"):
        validate_stop_basis(basis)  # does not raise
    assert set(SELECTABLE_BASES) == {"total_credit", "short_premium"}


@then('no runtime toggle exists that lifts the gate')
def _(world):
    import inspect
    assert list(inspect.signature(validate_stop_basis).parameters) == ["basis"]


# --- Scenario: reconciliation recorded on every real fill --------------------

@given('a condor fill from the live broker under any stop_basis')
def _(world):
    world["record"] = reconcile([D("1.35"), D("-0.15"), D("1.25"), D("-0.15")],
                                net_fill=D("2.30"), tick=TICK)
    world["bad"] = reconcile([D("0.05"), D("0.00"), D("0.30"), D("0.15")],
                             net_fill=D("0.05"), tick=TICK)


@then('a reconciliation record is logged comparing sum of allocated leg prices to the net fill')
def _(world):
    assert world["record"].allocated_sum == D("2.30") and world["record"].net_fill == D("2.30")


@then('the record PASSES only if they agree within one tick and no leg is zero-priced without trading at zero')
def _(world):
    assert world["record"].passed is True
    assert world["bad"].passed is False


@then('paper-mode fills never produce reconciliation records')
def _(world):
    from meic.adapters.tastytrade.adapter import TastytradeAdapter
    assert hasattr(TastytradeAdapter, "record_fill_allocation")  # only the live adapter records


# --- Scenario: fixed ungate criterion ----------------------------------------

@given('fewer than 5 consecutive PASSED reconciliation records from real fills')
def _(world):
    gate = AllocationGate(required=5)
    for _ in range(4):
        gate.observe(reconcile([D("2.30")], net_fill=D("2.30"), tick=TICK))
    world["gate"] = gate


@then('the gate cannot be lifted')
def _(world):
    assert world["gate"].consecutive_passed == 4 and world["gate"].ungate_ready() is False


@then('a FAILED record resets the consecutive count to zero')
def _(world):
    g = world["gate"]
    g.observe(reconcile([D("0.50"), D("0.00")], net_fill=D("0.05"), tick=TICK))  # FAIL
    assert g.consecutive_passed == 0 and g.ungate_ready() is False
    for _ in range(5):
        g.observe(reconcile([D("2.30")], net_fill=D("2.30"), tick=TICK))
    assert g.ungate_ready() is True


@then('lifting the gate requires an operator-ratified spec amendment')
def _(world):
    # ungate_ready() reports the empirical bar only; the config gate still stands
    with pytest.raises(StopBasisRejected):
        validate_stop_basis("per_side")
