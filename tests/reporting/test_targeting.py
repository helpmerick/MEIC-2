"""reporting.targeting — RPT-05 targeting decomposition, pure arithmetic."""
from decimal import Decimal as D

from meic.reporting.targeting import execution_gap, selection_gap, wing_drag


def test_selection_gap_is_matched_probe_minus_target():
    assert selection_gap(D("2.95"), D("3.00")) == D("-0.05")


def test_execution_gap_is_short_fill_minus_selected_mid():
    assert execution_gap(D("2.93"), D("2.95")) == D("-0.02")


def test_wing_drag_is_gross_short_minus_net_credit_per_side():
    assert wing_drag(D("2.95"), D("2.60")) == D("0.35")
