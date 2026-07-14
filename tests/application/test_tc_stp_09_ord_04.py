"""TC-STP-09 (superseded, see below) and TC-ORD-04 (EC-ENT-06 partial fill).
Pure decisions against the partial-fill module."""
from meic.application.partial_fill import (
    resolve_balanced_partial,
    resolve_unbalanced,
)


# --- TC-STP-09: SUPERSEDED (STP-03 v1.67 tombstone) --------------------------
#
# TC-STP-09 originally pinned EC-STP-08's stop_limit unfilled-escalation
# watchdog. The 07-13 week-review found that EC-STP-08 was never wired to
# anything live -- application/stop_escalation.py existed, was unit-tested
# (this file, until now), and had exactly two references repo-wide: itself
# and this test. STP-03 (v1.67, operator-ratified) tombstoned stop_limit
# outright ("retire, don't build"): the module is DELETED, and the absence is
# now what TC-NFR-07 scenario 2 tests -- no code constructs a stop_limit order
# and the config loader rejects `stop_order_type`. See
# tests/application/test_tc_nfr_07_stp03_tombstone.py.


# --- TC-ORD-04: partial fill of the 4-leg complex order ----------------------

def test_tc_ord_04a_balanced_partial_keeps_and_protects_filled_condors():
    """TC-ORD-04 (a): 1 of 2 condors filled at cancel ⇒ keep the filled condor,
    place its stops, record the reduced quantity."""
    plan = resolve_balanced_partial(ordered_condors=2, filled_condors=1)
    assert plan.keep_condors == 1 and plan.recorded_qty == 1 and plan.place_stops is True

    # nothing filled -> nothing kept, no stops
    none = resolve_balanced_partial(ordered_condors=2, filled_condors=0)
    assert none.keep_condors == 0 and none.place_stops is False


def test_tc_ord_04b_unbalanced_anomaly_completes_then_flattens_with_alert():
    """TC-ORD-04 (b): an unbalanced-leg anomaly ⇒ attempt completion for
    partial_fix_seconds, else flatten the filled legs; never carry past 2×;
    critical alert on any unbalanced discovery."""
    within = resolve_unbalanced(seconds_since_detect=5, partial_fix_seconds=15)
    assert within["action"] == "attempt_completion"
    assert within["alert"] == ("critical", "unbalanced_position")

    after = resolve_unbalanced(seconds_since_detect=20, partial_fix_seconds=15)
    assert after["action"] == "flatten_filled_legs"
    assert after["alert"] == ("critical", "unbalanced_position")

    # hard cap: never carry an unbalanced position past partial_fix_seconds × 2
    capped = resolve_unbalanced(seconds_since_detect=31, partial_fix_seconds=15)
    assert capped["action"] == "flatten_filled_legs" and capped["hard_cap"] is True
