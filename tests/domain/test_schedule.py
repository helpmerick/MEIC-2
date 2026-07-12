"""Schedule composition + validation (UC-02, doc 06 §37 + validation rules, v1.44).

These tests exist because a bad schedule is a real-money hazard. Every one of
them is a thing that must be UN-ARM-ABLE.
"""
from datetime import time
from decimal import Decimal as D

import pytest

from meic.domain.risk import day_worst_case, exceeds_max_day_risk, worst_case_loss
from meic.domain.schedule import (
    EntrySpec,
    ScheduleDefaults,
    may_arm,
    resolve,
    validate_entry,
    validate_schedule,
)

DEFAULTS = ScheduleDefaults()
SESSION = dict(session_open=time(9, 30), session_close=time(16, 0),
               min_time_before_close_minutes=30)


def _errs(entries, **kw):
    return validate_schedule(entries, DEFAULTS, **{**SESSION, **kw})


def _reasons(entries, **kw):
    return {(e.index, e.field, e.reason) for e in _errs(entries, **kw)}


# --- inheritance: unset fields take the global (doc 06 §37) --------------------

def test_unset_fields_inherit_globals_and_overrides_win():
    r = resolve(EntrySpec(time=time(10, 0)), DEFAULTS)
    assert r.contracts == 1 and r.target_premium == D("3.00") and r.wing_width == D("50")
    assert r.stop_loss_pct == 95 and r.stop_basis == "total_credit"
    assert r.probe_down_max == 25

    over = resolve(EntrySpec(time=time(10, 0), contracts=2, target_premium=D("2.25"),
                             wing_width=D("30"), stop_loss_pct=100, probe_down_max=15), DEFAULTS)
    assert over.contracts == 2 and over.target_premium == D("2.25")
    assert over.wing_width == D("30") and over.stop_loss_pct == 100 and over.probe_down_max == 15
    assert over.stop_basis == "total_credit"  # still inherited


def test_a_valid_schedule_arms():
    rows = [EntrySpec(time=time(10, 0), contracts=2), EntrySpec(time=time(11, 15))]
    assert _errs(rows) == []
    assert may_arm(rows, DEFAULTS, **SESSION) is True


# --- schedule-level rules (doc 06 rule 3) -------------------------------------

def test_empty_schedule_is_rejected():
    assert _reasons([]) == {(None, "entries", "empty_schedule")}
    assert may_arm([], DEFAULTS, **SESSION) is False


def test_times_must_be_strictly_increasing():
    same = [EntrySpec(time=time(10, 0)), EntrySpec(time=time(10, 0))]
    assert (1, "time", "not_strictly_increasing") in _reasons(same)
    backwards = [EntrySpec(time=time(11, 0)), EntrySpec(time=time(10, 0))]
    assert (1, "time", "not_strictly_increasing") in _reasons(backwards)


@pytest.mark.parametrize("t", [time(9, 29), time(16, 0), time(16, 1), time(4, 0)])
def test_times_outside_market_hours_are_rejected(t):
    assert (0, "time", "outside_market_hours") in _reasons([EntrySpec(time=t)])


def test_the_open_itself_is_inside_market_hours():
    assert _errs([EntrySpec(time=time(9, 30))]) == []


def test_an_entry_too_close_to_the_close_is_rejected():
    """The real-money hazard: an entry 30 seconds before the bell."""
    assert (0, "time", "too_close_to_close") in _reasons([EntrySpec(time=time(15, 59))])
    assert (0, "time", "too_close_to_close") in _reasons([EntrySpec(time=time(15, 31))])
    assert _errs([EntrySpec(time=time(15, 30))]) == []  # exactly min_time_before_close: OK


def test_an_early_close_moves_the_cutoff():
    half_day = dict(SESSION, session_close=time(13, 0))
    assert _errs([EntrySpec(time=time(12, 30))], **half_day) == []
    assert (0, "time", "too_close_to_close") in _reasons([EntrySpec(time=time(12, 45))], **half_day)
    assert (0, "time", "outside_market_hours") in _reasons([EntrySpec(time=time(14, 0))], **half_day)


# --- per-entry ranges, applied AFTER inheritance ------------------------------

@pytest.mark.parametrize("contracts", [0, 11, -1, 100])
def test_contracts_outside_1_to_10_are_rejected(contracts):
    """v1.44 narrowed this from 1-100 to 1-10."""
    assert (0, "contracts", "out_of_range") in _reasons([EntrySpec(time=time(10, 0), contracts=contracts)])


@pytest.mark.parametrize("contracts", [1, 2, 10])
def test_contracts_1_to_10_are_accepted(contracts):
    assert _errs([EntrySpec(time=time(10, 0), contracts=contracts)]) == []


@pytest.mark.parametrize("premium", [D("0.10"), D("0.49"), D("20.01"), D("50")])
def test_target_premium_out_of_range_rejected(premium):
    """A $0.10 premium target must be un-arm-able."""
    assert (0, "target_premium", "out_of_range") in _reasons(
        [EntrySpec(time=time(10, 0), target_premium=premium)])


def test_wing_width_range_and_step():
    assert (0, "wing_width", "out_of_range") in _reasons([EntrySpec(time=time(10, 0), wing_width=D("5"))])
    assert (0, "wing_width", "out_of_range") in _reasons([EntrySpec(time=time(10, 0), wing_width=D("205"))])
    assert (0, "wing_width", "bad_step") in _reasons([EntrySpec(time=time(10, 0), wing_width=D("32"))])
    assert _errs([EntrySpec(time=time(10, 0), wing_width=D("30"))]) == []


@pytest.mark.parametrize("pct", [94, 96, 301, 0])
def test_stop_loss_pct_must_be_in_the_discrete_set(pct):
    assert (0, "stop_loss_pct", "not_in_set") in _reasons([EntrySpec(time=time(10, 0), stop_loss_pct=pct)])


def test_per_side_stop_basis_is_gated_not_merely_unknown():
    """STP-02d: per_side is refused with its own reason, not 'not_in_set'."""
    assert (0, "stop_basis", "allocation_unverified") in _reasons(
        [EntrySpec(time=time(10, 0), stop_basis="per_side")])
    assert (0, "stop_basis", "not_in_set") in _reasons(
        [EntrySpec(time=time(10, 0), stop_basis="bogus")])
    for ok in ("total_credit", "short_premium"):
        assert _errs([EntrySpec(time=time(10, 0), stop_basis=ok)]) == []


def test_stop_rebate_markup_range_and_step():
    assert (0, "stop_rebate_markup", "out_of_range") in _reasons(
        [EntrySpec(time=time(10, 0), stop_rebate_markup=D("5.05"))])
    assert (0, "stop_rebate_markup", "bad_step") in _reasons(
        [EntrySpec(time=time(10, 0), stop_rebate_markup=D("0.07"))])
    assert _errs([EntrySpec(time=time(10, 0), stop_rebate_markup=D("0.50"))]) == []


@pytest.mark.parametrize("n", [0, 41])
def test_probe_down_max_range(n):
    """v1.44: probe_down_max joins the per-entry overrides (the $0.75 = 15 x 0.05)."""
    assert (0, "probe_down_max", "out_of_range") in _reasons(
        [EntrySpec(time=time(10, 0), probe_down_max=n)])


def test_probe_down_max_15_is_the_operators_075_dollar_bound():
    assert _errs([EntrySpec(time=time(10, 0), probe_down_max=15)]) == []
    assert D("15") * D("0.05") == D("0.75")  # display dollars == n x lattice


def test_min_premium_and_credit_floors():
    assert (0, "min_short_premium", "out_of_range") in _reasons(
        [EntrySpec(time=time(10, 0), min_short_premium=D("0.01"))])
    assert (0, "min_total_credit", "out_of_range") in _reasons(
        [EntrySpec(time=time(10, 0), min_total_credit=D("0.05"))])


def test_strike_method_and_delta_target():
    assert (0, "strike_method", "not_in_set") in _reasons(
        [EntrySpec(time=time(10, 0), strike_method="vibes")])
    assert (0, "short_delta_target", "out_of_range") in _reasons(
        [EntrySpec(time=time(10, 0), short_delta_target=D("0.50"))])


# --- every offending field is reported, not just the first --------------------

def test_validation_reports_all_errors_across_rows_and_fields():
    rows = [
        EntrySpec(time=time(9, 0), contracts=0, wing_width=D("7")),   # 3 problems
        EntrySpec(time=time(8, 0), stop_loss_pct=97),                 # 2 problems
    ]
    reasons = _reasons(rows)
    assert (0, "time", "outside_market_hours") in reasons
    assert (0, "contracts", "out_of_range") in reasons
    assert (0, "wing_width", "out_of_range") in reasons
    assert (1, "time", "not_strictly_increasing") in reasons
    assert (1, "stop_loss_pct", "not_in_set") in reasons
    assert may_arm(rows, DEFAULTS, **SESSION) is False


# --- RSK-04: SUM of per-entry worst cases, never n x max (amended TC-ENT-03) ---

def test_day_worst_case_sums_per_entry_never_n_times_max():
    """Rows of 2 and 1 contracts: 2*wc1 + 1*wc2, NEVER 3*max(wc)."""
    e1 = (D("50"), D("4.00"), 2)   # (50-4)*100*2 = 9200
    e2 = (D("30"), D("2.00"), 1)   # (30-2)*100*1 = 2800
    assert worst_case_loss(D("50"), D("4.00"), contracts=2) == D("9200")
    assert worst_case_loss(D("30"), D("2.00"), contracts=1) == D("2800")

    total = day_worst_case([e1, e2])
    assert total == D("12000")                      # 9200 + 2800

    n_times_max = 3 * max(D("9200"), D("2800"))     # the WRONG model
    assert total != n_times_max and n_times_max == D("27600")


def test_rsk04_blocks_when_summed_exposure_exceeds_the_cap():
    open_wcs = [worst_case_loss(D("50"), D("4.00"), contracts=2)]   # 9200
    new_wc = worst_case_loss(D("30"), D("2.00"), contracts=1)       # 2800
    assert exceeds_max_day_risk(open_wcs, new_wc, D("12000")) is False  # exactly at cap
    assert exceeds_max_day_risk(open_wcs, new_wc, D("11999")) is True   # over


def test_worst_case_is_per_side_not_both():
    """(width - credit) x 100 x contracts — only one side can settle ITM."""
    assert worst_case_loss(D("50"), D("4.00"), contracts=1) == D("4600")
    assert worst_case_loss(D("50"), D("4.00"), contracts=1) != D("9200")  # not both sides
