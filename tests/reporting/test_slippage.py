"""reporting.slippage — RPT-06 slippage-in / RPT-07 slippage-out families."""
from decimal import Decimal as D

from meic.reporting.slippage import maximum, mean, p50, p90, slippage_in, stop_slippage


def test_slippage_in_is_positive_for_price_improvement():
    assert slippage_in(D("3.50"), D("3.60")) == D("0.10")


def test_slippage_in_is_negative_for_a_worse_fill():
    assert slippage_in(D("3.50"), D("3.40")) == D("-0.10")


def test_stop_slippage_dollars_and_ticks():
    dollars, ticks = stop_slippage(D("3.80"), D("3.90"))
    assert dollars == D("0.10")
    assert ticks == D("2")


def test_aggregates_over_multiple_samples():
    samples = [D("0.05"), D("0.10"), D("0.15"), D("0.20")]
    assert mean(samples) == D("0.125")
    assert p50(samples) == D("0.10")
    assert p90(samples) == D("0.20")
    assert maximum(samples) == D("0.20")


def test_aggregates_on_empty_list_are_none():
    assert mean([]) is None
    assert p50([]) is None
    assert p90([]) is None
    assert maximum([]) is None
