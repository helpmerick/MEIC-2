"""reporting.metrics — edge cases beyond the pinned TC-RPT-02 vector
(tests/bdd/test_tc_rpt_02.py): undefined ratios never fabricate a number,
Sortino gates identically to Sharpe, drawdown on an all-winning curve is
zero, longest losing streak, avg win/loss."""
from decimal import Decimal as D

from meic.reporting import metrics

BASE = D("10000")


def test_profit_factor_is_none_with_no_losing_periods():
    assert metrics.profit_factor([D("100"), D("200")]) is None


def test_expectancy_is_none_for_an_empty_sequence():
    assert metrics.expectancy([]) is None


def test_day_win_rate_is_none_for_an_empty_sequence():
    assert metrics.day_win_rate([]) is None


def test_max_drawdown_is_zero_on_an_all_winning_curve():
    dollars, pct = metrics.max_drawdown([D("100"), D("50"), D("25")], BASE)
    assert dollars == D("0") and pct == D("0")


def test_avg_win_and_avg_loss():
    values = [D("100"), D("-50"), D("200"), D("-30")]
    assert metrics.avg_win(values) == D("150")   # mean(100, 200)
    assert metrics.avg_loss(values) == D("-40")  # mean(-50, -30)


def test_avg_win_none_when_there_are_no_wins():
    assert metrics.avg_win([D("-10"), D("-20")]) is None


def test_avg_loss_none_when_there_are_no_losses():
    assert metrics.avg_loss([D("10"), D("20")]) is None


def test_longest_losing_streak():
    values = [D("10"), D("-5"), D("-5"), D("-5"), D("10"), D("-5")]
    assert metrics.longest_losing_streak(values) == 3


def test_longest_losing_streak_is_zero_with_no_losses():
    assert metrics.longest_losing_streak([D("10"), D("20")]) == 0


def test_sortino_gates_identically_to_sharpe():
    short_series = [D("10")] * 5
    assert metrics.sortino(short_series, BASE, min_sample_days=20) is None


def test_sortino_is_none_with_no_downside():
    long_series = [D("10")] * 25   # all positive -> zero downside deviation
    assert metrics.sortino(long_series, BASE, min_sample_days=20) is None


def test_sortino_penalizes_downside_only():
    series = [D("400"), D("20"), D("-360"), D("400"), D("20")] * 4  # 20 periods
    result = metrics.sortino(series, BASE, min_sample_days=20)
    assert result is not None and result > 0
