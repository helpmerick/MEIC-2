"""reporting.waterfall — edge cases beyond the pinned TC-RPT-07 vector
(tests/bdd/test_tc_rpt_07.py): a matching expected_net never raises, and
premium_capture is None (never a fabricated ratio) with zero credit."""
from decimal import Decimal as D

import pytest

from meic.reporting.waterfall import WaterfallResidualError, build_waterfall


def test_matching_expected_net_does_not_raise():
    wf = build_waterfall(credits=D("8400"), stop_costs=D("2600"), recoveries=D("310"),
                          buybacks=D("145"), fees=D("220"), slippage=D("95"),
                          expected_net=D("5650"))
    assert wf.net == D("5650")


def test_no_expected_net_supplied_never_raises():
    wf = build_waterfall(credits=D("100"), stop_costs=D("0"), recoveries=D("0"),
                          buybacks=D("0"), fees=D("0"), slippage=D("0"))
    assert wf.net == D("100")


def test_premium_capture_is_none_with_zero_credit():
    wf = build_waterfall(credits=D("0"), stop_costs=D("0"), recoveries=D("0"),
                          buybacks=D("0"), fees=D("0"), slippage=D("0"))
    assert wf.premium_capture is None


def test_residual_error_carries_both_values():
    with pytest.raises(WaterfallResidualError) as exc_info:
        build_waterfall(credits=D("100"), stop_costs=D("0"), recoveries=D("0"),
                        buybacks=D("0"), fees=D("0"), slippage=D("0"),
                        expected_net=D("99"))
    err = exc_info.value
    assert err.expected_net == D("99") and err.computed_net == D("100")
    assert err.residual == D("1")


# --- EOD-01 v1.59: settlements bar --------------------------------------------

def test_settlements_bar_defaults_to_zero_and_never_affects_pre_v1_59_callers():
    wf = build_waterfall(credits=D("8400"), stop_costs=D("2600"), recoveries=D("310"),
                          buybacks=D("145"), fees=D("220"), slippage=D("95"),
                          expected_net=D("5650"))
    assert wf.settlements == D("0")


def test_settlements_bar_reconciles_the_pinned_2026_07_09_vector():
    """credits 360.00 (3.60 credit x100), fees 4.88, settlements -369.00 (the
    C7540 cash-settled assignment, already net of its own $5 fee) -> net
    -13.88, exactly the day's true broker-confirmed number."""
    wf = build_waterfall(credits=D("360.00"), stop_costs=D("0"), recoveries=D("0"),
                          buybacks=D("0"), fees=D("4.88"), slippage=D("0"),
                          settlements=D("-369.00"), expected_net=D("-13.88"))
    assert wf.net == D("-13.88")
    assert wf.settlements == D("-369.00")
