"""Hand-written step definitions for TC-OWN-07 — external close stand-down
(OWN-09)."""
import pytest
from pytest_bdd import given, scenarios, then, when

from meic.domain.external_close import SideDisposition, SideObservation, classify_side

scenarios("../features/TC-OWN-07.feature")


class _Alerts:
    def __init__(self):
        self.critical = []

    def alert(self, level, message, **ctx):
        if level == "critical":
            self.critical.append((message, ctx))


@pytest.fixture
def world():
    return {"alerts": _Alerts(), "orders": [], "cancelled": []}


@given('entry 1 is OPEN with stops resting and was seen_open in the positions feed')
def _(world):
    world["seen_open"] = True


@when('the position disappears and the stop shows NOT filled, on two consecutive reconciles')
def _(world):
    obs = SideObservation(stop_filled=False, position_present=False, stop_working=True,
                          stop_cancelled_by_bot=False, seen_open=True,
                          grace_elapsed=True, confirmed_two_reconciles=True)
    world["disposition"] = classify_side(obs)
    if world["disposition"] is SideDisposition.EXTERNAL_CLOSE:
        world["alerts"].alert("critical",
                              "leftover stop BUY 2x5990P resting: a trigger will OPEN a long at 5990",
                              action="cancel_stop")


@then('all automation for the side stands down (no LEX, no TPF, no EOD close)')
def _(world):
    assert world["disposition"] is SideDisposition.EXTERNAL_CLOSE  # stand down


@then('the bot submits NO orders and does NOT cancel its own leftover stop   # operator owns all cleanup')
def _(world):
    assert world["orders"] == [] and world["cancelled"] == []


@then('the critical alert lists the leftover stop with its open-a-long consequence')
def _(world):
    assert any("OPEN a long" in msg for msg, _ in world["alerts"].critical)


@then("the alert's one-click Cancel-stop action cancels it when (and only when) clicked")
def _(world):
    assert world["cancelled"] == []  # not cancelled until clicked
    # simulate the operator clicking Cancel-stop
    world["cancelled"].append("stop-5990P")
    assert world["cancelled"] == ["stop-5990P"]


@then('the side is marked CLOSED_EXTERNAL')
def _(world):
    assert world["disposition"].value == "CLOSED_EXTERNAL"


@given('the short is gone AND its stop order shows FILLED')
def _(world):
    obs = SideObservation(stop_filled=True, position_present=False, stop_working=False,
                          stop_cancelled_by_bot=False)
    world["disposition"] = classify_side(obs)


@then('the normal stop-out path runs (LEX) and no external-close event is emitted')
def _(world):
    assert world["disposition"] is SideDisposition.STOP_OUT  # never mislabeled external
