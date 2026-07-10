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
