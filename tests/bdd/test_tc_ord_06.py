"""Hand-written step definitions for TC-ORD-06 — the ORD-08 cancel-failure
taxonomy: terminal never retries, transient retries bounded, filled routes to
fill, unclassifiable escalates."""
import pytest
from pytest_bdd import given, scenarios, then

from meic.application.cancel_taxonomy import cancel_action

scenarios("../features/TC-ORD-06.feature")


@pytest.fixture
def world():
    return {}


# --- Scenario 1: terminal ----------------------------------------------------

@given('a resting stop whose cancel fails with "order no longer exists"')
def _(world):
    world["action"] = cancel_action("order no longer exists")


@then('the order is marked dead and tracking stops')
def _(world):
    a = world["action"]
    assert a["kind"] == "terminal" and a["mark_dead"] is True and a["stop_tracking"] is True


@then('the cancel is never retried and protection is never re-added for a dead order')
def _(world):
    a = world["action"]
    assert a["retry"] is False and a["re_add_protection"] is False


@then('total requests for that order after the terminal response = 0')
def _(world):
    assert world["action"]["retry"] is False  # no retry -> zero further requests


# --- Scenario 2: transient + filled ------------------------------------------

@given('cancels failing with timeouts, then a cancel rejected because filled')
def _(world):
    world["timeout"] = cancel_action("timeout")
    world["filled"] = cancel_action("already_filled")


@then('the timeout case retries with backoff up to its cap')
def _(world):
    a = world["timeout"]
    assert a["kind"] == "transient" and a["retry"] is True
    assert a["backoff"] is True and a["hard_cap"] is True


@then('the filled case is handled as a fill (EC-API-06)')
def _(world):
    a = world["filled"]
    assert a["kind"] == "filled" and a["route_as_fill"] is True and a["retry"] is False


# --- Scenario 3: unclassifiable ----------------------------------------------

@given('a cancel failure matching no known class')
def _(world):
    world["action"] = cancel_action("some_never_seen_broker_error")


@then('it is treated as transient with a hard retry cap and raises an alert at the cap')
def _(world):
    a = world["action"]
    assert a["kind"] == "unclassifiable"
    assert a["retry"] is True and a["hard_cap"] is True and a["alert"] is True
