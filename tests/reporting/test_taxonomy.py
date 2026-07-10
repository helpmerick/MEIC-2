"""reporting.taxonomy — RPT-03 outcome classification, beyond the two pinned
BDD scenarios (tests/bdd/test_tc_rpt_03.py): every outcome classifies exactly
once, and an open (unsettled) entry has no outcome yet."""
from decimal import Decimal as D

from meic.domain.projection import EntryProjection
from meic.reporting.taxonomy import (
    BOTH_SIDES_STOPPED,
    DECAY_CLOSE,
    EOD_CLOSE,
    EXTERNAL,
    FULL_EXPIRY,
    INFEASIBLE_STOP,
    MANUAL_CLOSE,
    MANUAL_FLATTEN,
    ONE_SIDE_STOPPED,
    TPF_CLOSE,
    classify,
    contract_audit,
)


def test_open_entry_has_no_outcome_yet():
    assert classify(EntryProjection(entry_id="e1", net_credit=D("4.00"))) is None


def test_full_expiry_both_sides_expired_untouched():
    e = EntryProjection(entry_id="e1", sides_expired=("PUT", "CALL"))
    assert classify(e) == FULL_EXPIRY


def test_one_side_stopped_other_side_still_open():
    e = EntryProjection(entry_id="e1", sides_stopped=("PUT",))
    assert classify(e) == ONE_SIDE_STOPPED


def test_both_sides_stopped():
    e = EntryProjection(entry_id="e1", sides_stopped=("PUT", "CALL"))
    assert classify(e) == BOTH_SIDES_STOPPED


def test_close_initiators_map_one_to_one():
    cases = {
        "manual": MANUAL_CLOSE,
        "manual_flatten": MANUAL_FLATTEN,
        "take_profit": TPF_CLOSE,
        "eod": EOD_CLOSE,
        "infeasible_stop": INFEASIBLE_STOP,
    }
    for initiator, outcome in cases.items():
        e = EntryProjection(entry_id="e1", close_initiator=initiator)
        assert classify(e) == outcome, initiator


def test_decay_close_wins_even_if_a_side_also_stopped():
    """DCY-02 is a short-only close; the OTHER side may have stopped normally
    first. Either signal (close_initiator=='decay' or 'decay' in
    stop_initiators) must classify DECAY_CLOSE, never double-counted as
    ONE_SIDE_STOPPED too (RPT-03: exactly once)."""
    e = EntryProjection(entry_id="e1", sides_stopped=("PUT",),
                        stop_initiators=("resting_stop",), close_initiator="decay")
    assert classify(e) == DECAY_CLOSE

    e2 = EntryProjection(entry_id="e2", sides_stopped=("PUT",), stop_initiators=("decay",))
    assert classify(e2) == DECAY_CLOSE


def test_unrecognized_initiator_falls_through_to_external():
    e = EntryProjection(entry_id="e1", close_initiator="operator_at_broker")
    assert classify(e) == EXTERNAL


def test_contract_audit_only_applies_to_the_two_stopped_outcomes():
    assert contract_audit(EntryProjection(entry_id="e1", sides_expired=("PUT", "CALL")),
                          pct=D("0.95")) is None
    assert contract_audit(EntryProjection(entry_id="e1", close_initiator="manual"),
                          pct=D("0.95")) is None
    assert contract_audit(EntryProjection(entry_id="e1"), pct=D("0.95")) is None  # still open
