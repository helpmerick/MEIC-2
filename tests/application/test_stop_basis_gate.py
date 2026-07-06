"""STP-02d config gate + allocation reconciler units."""
from decimal import Decimal as D

import pytest

from meic.config.stop_basis import StopBasisRejected, validate_stop_basis
from meic.domain.allocation import AllocationGate, reconcile

TICK = D("0.05")


def test_per_side_rejected_total_and_short_premium_ok():
    with pytest.raises(StopBasisRejected) as ei:
        validate_stop_basis("per_side")
    assert ei.value.reason == "allocation_unverified"
    validate_stop_basis("total_credit")
    validate_stop_basis("short_premium")


def test_unknown_basis_rejected():
    with pytest.raises(StopBasisRejected) as ei:
        validate_stop_basis("mid")
    assert ei.value.reason == "unknown_basis"


def test_reconcile_pass_within_tick():
    r = reconcile([D("1.35"), D("-0.15"), D("1.25"), D("-0.15")], net_fill=D("2.28"), tick=TICK)
    assert r.passed  # 2.30 vs 2.28 within one 0.05 tick


def test_reconcile_fail_sum_mismatch():
    r = reconcile([D("1.35"), D("-0.15")], net_fill=D("0.50"), tick=TICK)
    assert not r.passed and r.reason == "sum_mismatch"


def test_reconcile_fail_phantom_zero_leg():
    r = reconcile([D("2.30"), D("0.00")], net_fill=D("2.30"), tick=TICK)
    assert not r.passed and r.reason == "phantom_zero_priced_leg"
    # a leg that genuinely traded at zero is allowed
    ok = reconcile([D("2.30"), D("0.00")], net_fill=D("2.30"), tick=TICK,
                   legs_that_traded_at_zero=frozenset({1}))
    assert ok.passed


def test_gate_streak_and_reset():
    g = AllocationGate(required=5)
    for _ in range(5):
        g.observe(reconcile([D("2.30")], net_fill=D("2.30"), tick=TICK))
    assert g.ungate_ready()
    g.observe(reconcile([D("0.50")], net_fill=D("0.05"), tick=TICK))  # FAIL
    assert not g.ungate_ready() and g.consecutive_passed == 0
