"""TC-RPT-07 — the RPT-11 waterfall, pinned. credits 8400, stop costs 2600,
recoveries 310, buybacks 145, fees 220, slippage 95 => net 5650, premium
capture 67.3%; a nonzero residual against an independently-known net renders
an explicit error state, never a silently adjusted bar."""
from decimal import ROUND_HALF_UP, Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.reporting.waterfall import WaterfallResidualError, build_waterfall

scenarios("../features/TC-RPT-07.feature")


@given("a period with credits 8400, stop costs 2600, recoveries 310, buybacks 145, fees 220, slippage 95",
       target_fixture="components")
def _():
    return dict(credits=D("8400"), stop_costs=D("2600"), recoveries=D("310"),
                buybacks=D("145"), fees=D("220"), slippage=D("95"))


@then("the waterfall bars sum exactly to the period net of 5650")
def _(components):
    wf = build_waterfall(**components)
    assert wf.net == D("5650")


@then("premium capture ratio = 67.3 percent")
def _(components):
    wf = build_waterfall(**components)
    pct = (wf.premium_capture * 100).quantize(D("0.1"), rounding=ROUND_HALF_UP)
    assert pct == D("67.3")


@then("any nonzero attribution residual renders an error state, never a silently adjusted bar")
def _(components):
    with pytest.raises(WaterfallResidualError) as exc_info:
        # An independently-known net (e.g. the day's own folded P&L) that
        # disagrees with the components' own arithmetic net by one cent.
        build_waterfall(**components, expected_net=D("5650.01"))
    assert exc_info.value.residual == D("-0.01")
