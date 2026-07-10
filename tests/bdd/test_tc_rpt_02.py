"""TC-RPT-02 — the canonical five-day RPT-04 vector, pinned. base 10,000;
dailies +400, +20, -360, +400, +20 => ROC 4.80%, Sharpe 4.79, MDD $360
(3.60%), PF 2.33, expectancy +$96/entry, day win rate 80%. Sharpe/Sortino
gate below `report_min_sample_days` (19 days -> insufficient data; ROC still
renders)."""
from decimal import ROUND_HALF_UP, Decimal as D

from pytest_bdd import given, parsers, scenarios, then

from meic.reporting import metrics

scenarios("../features/TC-RPT-02.feature")

BASE = D("10000")
DAILIES = [D("400"), D("20"), D("-360"), D("400"), D("20")]


def _round2(x: D) -> D:
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


@given(parsers.parse("capital base {base:d} and daily nets +400, +20, -360, +400, +20"),
       target_fixture="vector")
def _(base):
    assert D(base) == BASE
    return DAILIES


@then("ROC = 4.80 percent, annualized Sharpe = 4.79, max drawdown = 360 dollars (3.60 percent)")
def _(vector):
    roc = metrics.roc(vector, BASE) * 100
    assert _round2(roc) == D("4.80")

    sh = metrics.sharpe(vector, BASE, rf_pct=D("0"), min_sample_days=5)
    assert sh is not None and _round2(sh) == D("4.79")

    mdd_dollars, mdd_pct = metrics.max_drawdown(vector, BASE)
    assert mdd_dollars == D("360")
    assert _round2(mdd_pct * 100) == D("3.60")


@then("profit factor = 2.33, expectancy = +96 dollars per entry, day win rate = 80 percent")
def _(vector):
    pf = metrics.profit_factor(vector)
    assert pf is not None and _round2(pf) == D("2.33")

    exp = metrics.expectancy(vector)
    assert exp == D("96")

    wr = metrics.day_win_rate(vector)
    assert _round2(wr * 100) == D("80.00")


@given(parsers.parse("{n:d} trading days"), target_fixture="n_days")
def _(n):
    return n


@then('Sharpe and Sortino render "insufficient data" and ROC still renders')
def _(n_days):
    values = [D("10")] * n_days   # the specific values don't matter -- only the count gates
    assert metrics.sharpe(values, BASE, min_sample_days=20) is None
    assert metrics.sortino(values, BASE, min_sample_days=20) is None
    assert metrics.roc(values, BASE) is not None  # ROC never gates on sample size
