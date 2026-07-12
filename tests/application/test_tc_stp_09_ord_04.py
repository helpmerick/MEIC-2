"""TC-STP-09 (EC-STP-08 stop_limit escalation) and TC-ORD-04 (EC-ENT-06 partial
fill). Pure decisions against the escalation + partial-fill modules."""
from meic.application.partial_fill import (
    resolve_balanced_partial,
    resolve_unbalanced,
)
from meic.application.stop_escalation import (
    requires_unfilled_watchdog,
    should_escalate_to_market,
)


# --- TC-STP-09: stop_limit triggered-unfilled escalates to market ------------

def test_tc_stp_09_stop_limit_escalates_to_market_after_window():
    """TC-STP-09 (EC-STP-08): with stop_limit configured, a triggered-but-
    unfilled stop is cancelled/replaced with market after the escalation window;
    stop_market (default) never uses this path."""
    assert requires_unfilled_watchdog("stop_limit") is True
    assert requires_unfilled_watchdog("stop_market") is False

    # triggered, unfilled, past 10s -> escalate to market
    assert should_escalate_to_market(
        stop_order_type="stop_limit", triggered=True, filled=False,
        seconds_since_trigger=10, escalation_seconds=10) is True
    # not yet past the window -> hold
    assert should_escalate_to_market(
        stop_order_type="stop_limit", triggered=True, filled=False,
        seconds_since_trigger=9, escalation_seconds=10) is False
    # already filled -> nothing to escalate
    assert should_escalate_to_market(
        stop_order_type="stop_limit", triggered=True, filled=True,
        seconds_since_trigger=99, escalation_seconds=10) is False
    # stop_market never escalates through this watchdog
    assert should_escalate_to_market(
        stop_order_type="stop_market", triggered=True, filled=False,
        seconds_since_trigger=99, escalation_seconds=10) is False


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
