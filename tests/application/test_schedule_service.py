"""UC-02: compose -> validate -> version -> persist, and the arm pre-flight.

The panel's day-total is an ESTIMATE (v1.46): no strikes exist before selection,
so `(width - target premium) x 100 x contracts` is the best it can know. The
post-selection RSK-04 gate stays authoritative and can veto an entry the panel
showed as fitting. Both facts are asserted here.
"""
from decimal import Decimal as D

import pytest

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.application.preflight import run_preflight
from meic.application.schedule_service import (
    ScheduleService,
    day_total_estimate,
    worst_case_estimate,
)
from meic.domain.schedule import ResolvedEntry

OK = lambda: (True, "")
BAD = lambda: (False, "nope")


def _svc(rows=None, max_day_risk=None):
    state = PersistentState(InMemoryStateStore())
    if rows is not None:
        state.entry_schedule = rows
    if max_day_risk is not None:
        state.max_day_risk = str(max_day_risk)
    return ScheduleService(state)


def _row(t="10:00", **over):
    return {"time": t, **over}


# --- the UI-22 estimate ------------------------------------------------------------

def test_worst_case_estimate_uses_target_premium_not_a_real_credit():
    """No strikes exist at compose time, so the credit is the TARGET, not a fill."""
    from datetime import time as _t
    row = ResolvedEntry(time=_t(10, 0), contracts=2, target_premium=D("3.00"),
                        wing_width=D("50"), stop_loss_pct=95, stop_basis="total_credit",
                        stop_rebate_markup=D("0"), min_short_premium=D("1"),
                        min_total_credit=D("2"), probe_down_max=25,
                        strike_method="premium", short_delta_target=D("0.10"))
    assert worst_case_estimate(row) == D("9400")     # (50 - 3) x 100 x 2


def test_day_total_sums_per_entry_estimates_never_n_times_max():
    svc = _svc([_row("10:00", contracts=2), _row("11:00", contracts=1, wing_width="30")])
    resolved = svc.resolved()
    assert day_total_estimate(resolved) == D("9400") + D("2700")   # 12100
    assert day_total_estimate(resolved) != 3 * max(D("9400"), D("2700"))


def test_the_estimate_never_goes_negative():
    from datetime import time as _t
    row = ResolvedEntry(time=_t(10, 0), contracts=1, target_premium=D("60.00"),
                        wing_width=D("50"), stop_loss_pct=95, stop_basis="total_credit",
                        stop_rebate_markup=D("0"), min_short_premium=D("1"),
                        min_total_credit=D("2"), probe_down_max=25,
                        strike_method="premium", short_delta_target=D("0.10"))
    assert worst_case_estimate(row) == D("0")


# --- the panel view: max_day_risk beside the day total (v1.46 ruling 1) -------------

def test_adding_a_row_visibly_eats_headroom():
    svc = _svc([_row("10:00")], max_day_risk=D("20000"))
    before = svc.view()
    assert before.day_total_estimate == D("4700") and before.headroom == D("15300")

    svc._state.entry_schedule = [_row("10:00"), _row("11:00")]
    after = svc.view()
    assert after.day_total_estimate == D("9400") and after.headroom == D("10600")
    assert after.exceeds_max_day_risk is False


def test_the_view_warns_when_the_composed_day_exceeds_the_ceiling():
    svc = _svc([_row("10:00", contracts=5)], max_day_risk=D("20000"))   # 23500 est.
    view = svc.view()
    assert view.exceeds_max_day_risk is True
    assert view.headroom == D("-3500")


def test_an_unset_ceiling_is_not_unlimited_it_is_unknown():
    view = _svc([_row("10:00")]).view()
    assert view.max_day_risk is None and view.headroom is None
    assert view.exceeds_max_day_risk is False    # the panel cannot warn; RSK-04 still gates


def test_the_view_labels_its_number_as_an_estimate():
    d = _svc([_row("10:00")]).view().to_dict()
    assert "ESTIMATED" in d["estimate_note"] and "RSK-04" in d["estimate_note"]


# --- validate -> version -> persist ------------------------------------------------

def test_save_validates_before_persisting_anything():
    """An invalid schedule is never written: a half-saved one could arm on restart."""
    svc = _svc([])
    out = svc.save([_row("10:00", contracts=11)])          # ENT-04: 1-10
    assert out["result"] == "invalid"
    assert any(e["field"] == "contracts" for e in out["errors"])
    assert svc._state.entry_schedule == []                  # nothing persisted
    assert svc._state.config_version is None


def test_save_reports_every_error_not_just_the_first():
    out = _svc([]).save([_row("10:00", contracts=0), _row("09:00", stop_loss_pct=97)])
    fields = {e["field"] for e in out["errors"]}
    assert {"contracts", "stop_loss_pct"} <= fields
    assert any("increasing" in e["reason"] for e in out["errors"])  # 11:00 then 09:00


def test_save_bumps_the_config_version_monotonically():
    svc = _svc([])
    assert svc.save([_row("10:00")])["config_version"] == "v1"
    assert svc.save([_row("10:00"), _row("11:00")])["config_version"] == "v2"
    assert svc._state.config_version == "v2"


def test_save_persists_max_day_risk_from_the_panel():
    svc = _svc([])
    svc.save([_row("10:00")], max_day_risk="15000")
    assert svc.max_day_risk() == D("15000")
    assert svc.view().headroom == D("10300")


def test_an_empty_cell_inherits_the_global_it_is_not_zero():
    svc = _svc([{"time": "10:00", "contracts": "", "target_premium": None}])
    row = svc.resolved()[0]
    assert row.contracts == 1 and row.target_premium == D("3.00")   # ScheduleDefaults


def test_per_side_is_rejected_at_the_row_level():
    out = _svc([]).save([_row("10:00", stop_basis="per_side")])
    assert out["result"] == "invalid"
    assert any(e["reason"] == "allocation_unverified" for e in out["errors"])


def test_a_time_too_close_to_the_close_is_rejected():
    assert _svc([]).save([_row("15:31")])["result"] == "invalid"
    assert _svc([]).save([_row("15:30")])["result"] == "saved"


def test_an_unparsable_row_is_an_error_not_a_crash():
    # A non-time parse error (bad contracts) still routes to the generic row error.
    out = _svc([]).save([{"time": "10:00", "contracts": "abc"}])
    assert out["result"] == "invalid" and out["errors"][0]["field"] == "row"


# --- entry times must be 24-hour military, within market hours --------------------

@pytest.mark.parametrize("bad", ["1:53pm", "0930", "24:00", "11:60", "9:5", "noon", "11-53", ""])
def test_entry_time_must_be_24_hour_military(bad):
    """Entry times are 24-hour HH:MM; am/pm, 4-digit, out-of-range and unknown
    separators are refused with a precise per-row reason, not a generic crash."""
    out = _svc([]).save([_row(bad)])
    assert out["result"] == "invalid"
    err = out["errors"][0]
    assert err["field"] == "time" and err["reason"] == "not_24h_military" and err["index"] == 0


@pytest.mark.parametrize("dotted,canonical", [("11.53", "11:53"), ("9.32", "09:32"), ("15.30", "15:30")])
def test_a_dot_separator_is_accepted_and_canonicalised_to_a_colon(dotted, canonical):
    """People write times with a dot (UK-style 11.53). It means 11:53 — accept it
    and persist the canonical colon form."""
    out = _svc([]).save([_row(dotted)])
    assert out["result"] == "saved"
    assert out["rows"][0]["time"] == canonical


@pytest.mark.parametrize("good", ["09:32", "9:32", "10:00", "15:30", "23:59", "00:00"])
def test_military_times_parse_as_valid_format(good):
    """The format gate accepts any real 24-hour time; the SESSION gate (below) is
    what then rejects the ones outside market hours."""
    errs = _svc([]).validate([_row(good)])
    assert not any(e.reason == "not_24h_military" for e in errs)


def test_entry_time_must_be_within_market_hours():
    """Ruling: an entry time is only valid while the market is open (09:30-16:00 ET).
    Pre-market and after-close 24-hour times are refused even though the format is
    valid. (The DAY-02 30-min-before-close buffer is a separate, stricter gate.)"""
    assert _svc([]).save([_row("09:30")])["result"] == "saved"      # market open edge
    assert _svc([]).save([_row("08:00")])["result"] == "invalid"    # pre-market
    assert _svc([]).save([_row("16:30")])["result"] == "invalid"    # after close


# --- UC-02 pre-flight ---------------------------------------------------------------

def _pre(svc, **over):
    kw = dict(schedule_service=svc, reconcile_clear=OK, clock_ok=OK,
              config_ok=OK, market_data_ok=OK)
    return run_preflight(**{**kw, **over})


def test_preflight_passes_with_a_legal_schedule_and_clear_checks():
    pre = _pre(_svc([_row("10:00")]))
    assert pre.passed is True
    assert [c.name for c in pre.checks] == ["schedule", "reconcile", "clock",
                                            "config", "market_data"]


def test_arming_an_empty_schedule_is_rejected():
    pre = _pre(_svc([]))
    assert pre.passed is False and pre.first_failure.name == "schedule"
    assert "empty schedule" in pre.first_failure.detail
    assert len(pre.checks) == 1        # short-circuits: nothing else was even tried


def test_the_sequence_short_circuits_at_the_first_failure():
    """No point subscribing market data on top of an unresolved mismatch (REC-02)."""
    pre = _pre(_svc([_row("10:00")]), reconcile_clear=lambda: (False, "mismatch open"))
    assert [c.name for c in pre.checks] == ["schedule", "reconcile"]
    assert pre.to_dict()["blocked_by"] == "reconcile"


def test_a_check_that_raises_is_a_fail_not_an_exception():
    def boom():
        raise RuntimeError("broker down")

    pre = _pre(_svc([_row("10:00")]), clock_ok=boom)
    assert pre.passed is False and "broker down" in pre.first_failure.detail


def test_live_mode_requires_max_day_risk():
    """doc 06 s169: mandatory before live can be enabled."""
    pre = _pre(_svc([_row("10:00")]), require_max_day_risk=True)
    assert pre.passed is False and pre.first_failure.name == "max_day_risk"
    assert "mandatory" in pre.first_failure.detail

    ok = _pre(_svc([_row("10:00")], max_day_risk=D("20000")), require_max_day_risk=True)
    assert ok.passed is True


def test_live_preflight_refuses_a_composed_day_over_the_ceiling():
    pre = _pre(_svc([_row("10:00", contracts=5)], max_day_risk=D("20000")),
               require_max_day_risk=True)
    assert pre.passed is False and pre.first_failure.name == "max_day_risk"
    assert "exceeds" in pre.first_failure.detail


def test_paper_does_not_require_a_ceiling_but_never_calls_it_unlimited():
    pre = _pre(_svc([_row("10:00")]))                      # require_max_day_risk=False
    assert pre.passed is True
    assert "max_day_risk" not in [c.name for c in pre.checks]
    assert _svc([_row("10:00")]).max_day_risk() is None    # unknown, not infinite


# --- v1.47 pin-at-Save (operator-ratified, doc 06 section 37) -------------------

def _defaults(**over):
    from dataclasses import replace as dc_replace
    from meic.domain.schedule import ScheduleDefaults
    return dc_replace(ScheduleDefaults(), **over)


def _svc_with(state, defaults):
    return ScheduleService(state, defaults)


def test_a_saved_row_is_byte_identical_after_a_global_changes():
    """THE pin-at-Save invariant. Save a row against one set of globals, change
    every global, and the row's resolved parameters must not move by a hair.

    Same reasoning as STP-02's subsequent-entries-only rule: changing a setting
    can never silently change what a SAVED entry trades."""
    from meic.application.schedule_service import pinned_row

    state = PersistentState(InMemoryStateStore())
    before_globals = _defaults()                       # 1 contract, $3.00, 50 wide, 95%
    svc = _svc_with(state, before_globals)
    assert svc.save([_row("10:00")])["result"] == "saved"

    before = [pinned_row(r) for r in svc.resolved()]

    # the operator now changes every global that a row can inherit
    after_globals = _defaults(contracts=7, target_premium=D("9.00"), wing_width=D("100"),
                              stop_loss_pct=200, stop_basis="short_premium",
                              stop_rebate_markup=D("1.50"), min_short_premium=D("2.00"),
                              min_total_credit=D("5.00"), probe_down_max=40,
                              strike_method="delta", short_delta_target=D("0.25"))
    after = [pinned_row(r) for r in _svc_with(state, after_globals).resolved()]

    assert after == before, "a global change reached back into a saved row"
    assert before[0]["contracts"] == 1 and before[0]["target_premium"] == "3.00"
    assert before[0]["wing_width"] == "50" and before[0]["stop_loss_pct"] == 95


def test_the_saved_row_stores_concrete_values_for_every_parameter():
    """Nothing is left to inherit later — the row IS the contract."""
    state = PersistentState(InMemoryStateStore())
    _svc_with(state, _defaults()).save([_row("10:00")])

    stored = state.entry_schedule[0]
    assert set(stored) == {
        "time", "contracts", "target_premium", "wing_width", "stop_loss_pct",
        "stop_basis", "stop_rebate_markup", "min_short_premium", "min_total_credit",
        "probe_down_max", "strike_method", "short_delta_target", "id"}
    # every field concrete EXCEPT "id" is intentionally exempt from that check here:
    # it is a durable identity (ENT-10(4), v1.53), not an inherited parameter, and
    # this schedule's single fresh row legitimately gets id == 1.
    assert not any(v in (None, "") for k, v in stored.items() if k != "id")
    assert stored["id"] == 1


def test_globals_are_pre_fills_for_new_rows_only():
    """A NEW row still inherits whatever the globals are AT SAVE TIME — that is
    what a pre-fill means. It is only retro-application that is forbidden."""
    state = PersistentState(InMemoryStateStore())
    _svc_with(state, _defaults()).save([_row("10:00")])                  # pinned at 1
    rich = _svc_with(state, _defaults(contracts=4))
    rich.save([*state.entry_schedule, _row("11:00")])                    # new row appended

    assert [r.contracts for r in rich.resolved()] == [1, 4]              # old pinned, new pre-filled


def test_an_explicit_override_survives_the_pin_unchanged():
    state = PersistentState(InMemoryStateStore())
    svc = _svc_with(state, _defaults())
    svc.save([_row("10:00", contracts=3, stop_loss_pct=150, wing_width="30")])

    r = svc.resolved()[0]
    assert (r.contracts, r.stop_loss_pct, r.wing_width) == (3, 150, D("30"))


def test_decimals_pin_as_exact_strings_never_floats():
    """The order and the event log both depend on the exactness."""
    state = PersistentState(InMemoryStateStore())
    _svc_with(state, _defaults()).save([_row("10:00", target_premium="3.05",
                                             stop_rebate_markup="0.05")])
    stored = state.entry_schedule[0]
    assert stored["target_premium"] == "3.05" and isinstance(stored["target_premium"], str)
    assert stored["stop_rebate_markup"] == "0.05"


def test_a_row_composed_outside_the_panel_still_resolves():
    """The paper demo writes bare {"time": ...} rows; pre-v1.47 durable state may
    hold them too. They resolve against the globals rather than crashing."""
    state = PersistentState(InMemoryStateStore())
    state.entry_schedule = [{"time": "10:00"}]
    assert _svc_with(state, _defaults(contracts=2)).resolved()[0].contracts == 2


# --- ENT-10(4) v1.53: durable entry ids, assigned at Save -----------------------

def test_fresh_rows_get_sequential_ids_from_1():
    svc = _svc([])
    out = svc.save([_row("10:00"), _row("11:00"), _row("12:00")])
    assert [r["id"] for r in out["rows"]] == [1, 2, 3]


def test_a_row_with_an_explicit_id_keeps_it_verbatim():
    svc = _svc([])
    svc.save([_row("10:00"), _row("11:00")])
    edited = list(svc._state.entry_schedule)
    edited[1] = {**edited[1], "contracts": 3}          # edit row 2 in place, id carried
    out = svc.save(edited)
    assert [r["id"] for r in out["rows"]] == [1, 2]
    assert out["rows"][1]["contracts"] == 3


def test_deleting_a_fired_row_and_resaving_never_reuses_its_id():
    """THE operator ruling: A(fired)/B/C, delete A while ARMED, re-save — B and C
    must keep ids 2 and 3, and a brand new row must never become id 1 again."""
    svc = _svc([])
    svc.save([_row("10:00"), _row("11:15"), _row("12:35")])   # A=1, B=2, C=3
    rows = list(svc._state.entry_schedule)
    b, c = rows[1], rows[2]

    out = svc.save([b, c, _row("13:00")])   # A deleted, D newly composed
    ids = [r["id"] for r in out["rows"]]
    assert ids[:2] == [2, 3]                # B, C unchanged
    assert ids[2] == 4                      # D gets a FRESH id, never a reused 1


def test_used_ids_raises_the_floor_past_an_id_the_persisted_schedule_no_longer_carries():
    """used_ids models the caller's scan of TODAY's events (a fired row's id might
    not survive in the persisted schedule at all, e.g. it was deleted in an
    earlier save that predates this one) -- it must still never be reissued."""
    svc = _svc([])
    svc.save([_row("10:00")])          # id 1, then deleted in a prior save already
    out = svc.save([_row("11:00")], used_ids=5)   # events show id 5 already fired
    assert out["rows"][0]["id"] == 6


def test_new_and_kept_ids_never_collide_within_one_save():
    svc = _svc([])
    svc.save([_row("10:00"), _row("11:00"), _row("12:00")])   # 1, 2, 3
    rows = list(svc._state.entry_schedule)
    # keep row 3 (id 3), drop rows 1/2, add two new rows -- new ids must start at 4
    out = svc.save([rows[2], _row("13:00"), _row("14:00")])
    assert [r["id"] for r in out["rows"]] == [3, 4, 5]


def test_view_echoes_the_durable_id_and_a_pre_v153_row_shows_none():
    svc = _svc([])
    svc.save([_row("10:00")])
    assert svc.view().to_dict()["rows"][0]["id"] == 1

    legacy = _svc([{"time": "09:32"}])                  # pre-v1.53: no "id" key at all
    assert legacy.view().to_dict()["rows"][0]["id"] is None
