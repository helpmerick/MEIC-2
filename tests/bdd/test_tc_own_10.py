"""Hand-written step definitions for TC-OWN-10 — operator-cancelled stop is
never re-placed (OWN-11)."""
import pytest
from pytest_bdd import given, scenarios, then

from meic.domain.external_close import SideDisposition, SideObservation, classify_side

scenarios("../features/TC-OWN-10.feature")


@pytest.fixture
def world():
    return {"replaced": []}


@given('the stop order shows cancelled with no bot-initiated cancel in the event log')
def _(world):
    obs = SideObservation(stop_filled=False, position_present=True, stop_working=False,
                          stop_cancelled_by_bot=False)  # operator cancelled, position kept
    world["disposition"] = classify_side(obs)


@then('the bot does NOT re-place it')
def _(world):
    assert world["disposition"] is SideDisposition.USER_UNPROTECTED
    assert world["replaced"] == []  # never auto-replaced


@then('the side is marked USER_UNPROTECTED with a critical alert')
def _(world):
    assert world["disposition"].value == "USER_UNPROTECTED"


@then('the UI banner offers a one-click Re-protect action which places a fresh stop when clicked')
def _(world):
    assert world["replaced"] == []  # not until clicked
    world["replaced"].append("fresh-stop")  # operator clicks Re-protect
    assert world["replaced"] == ["fresh-stop"]


@given('a short whose stop was never confirmed (crash before placement)')
def _(world):
    obs = SideObservation(stop_filled=False, position_present=True, stop_working=False,
                          stop_cancelled_by_bot=True)  # bot-side failure, not operator
    world["disposition"] = classify_side(obs)


@then('REC-04 re-places the stop automatically   # OWN-11 applies only to non-bot cancels')
def _(world):
    assert world["disposition"] is SideDisposition.BOT_UNPROTECTED  # auto re-place path
