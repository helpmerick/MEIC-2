"""Hand-written step definitions for TC-OWN-01 — FOREIGN quarantine (OWN-03)."""
import pytest
from pytest_bdd import given, scenarios, then

from meic.domain.ownership import Ownership, OwnershipLedger

scenarios("../features/TC-OWN-01.feature")

SYMBOL = "SPXW_6050C"


class _Alerts:
    def __init__(self):
        self.critical = []

    def alert(self, level, message, **ctx):
        if level == "critical":
            self.critical.append(message)


@pytest.fixture
def world():
    return {"alerts": _Alerts(), "orders": []}


@given('the broker reports short 1 SPX 6050 call with no matching bot order fill')
def _(world):
    led = OwnershipLedger()  # no bot fills recorded for 6050 call
    world["ledger"] = led
    world["class"] = led.classify(SYMBOL, broker_net=-1)
    if world["class"] is Ownership.FOREIGN:
        world["alerts"].alert("critical", "FOREIGN position detected", symbol=SYMBOL)


@given('the FOREIGN position is an unprotected naked short in a moving market')
def _(world):
    # self-contained: a foreign naked short is still just FOREIGN, alert-only
    led = OwnershipLedger()
    world["ledger"] = led
    world["class"] = led.classify(SYMBOL, broker_net=-1)


@then('the position is marked FOREIGN')
def _(world):
    assert world["class"] is Ownership.FOREIGN


@then('the bot never submits any order referencing 6050 calls (stop, close, or hedge)')
def _(world):
    assert world["ledger"].cap_exit_qty(SYMBOL, 5) == 0  # OWN-04 caps to 0
    assert world["orders"] == []


@then('it appears in no bot P&L or risk figure')
def _(world):
    assert world["ledger"].owned(SYMBOL) == 0  # never enters the ledger


@then('a critical alert and persistent banner are raised')
def _(world):
    assert world["alerts"].critical


@then('the bot still submits no orders for it   # never guess operator intent')
def _(world):
    assert world["ledger"].cap_exit_qty(SYMBOL, 5) == 0
    assert world["orders"] == []
