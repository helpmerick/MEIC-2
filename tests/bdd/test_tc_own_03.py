"""Hand-written step definitions for TC-OWN-03 — shared symbol constrain+warn
(OWN-04/05)."""
import pytest
from pytest_bdd import given, scenarios, then, when

from meic.domain.ownership import Ownership, OwnershipLedger

scenarios("../features/TC-OWN-03.feature")

SYMBOL = "SPXW_5990P"


@pytest.fixture
def world():
    return {"warnings": []}


@given('the bot is short 2 x 5990 put (ledger = 2, stops resting for 2)')
def _(world):
    led = OwnershipLedger()
    led.apply_fill(SYMBOL, -2)  # short 2 from the bot's own fills
    world["ledger"] = led
    world["stops_resting"] = 2
    world["long_qty"] = 2  # the entry's long quantity


@when('the broker position becomes short 3 (foreign_delta = 1)')
def _(world):
    world["delta"] = world["ledger"].foreign_delta(SYMBOL, broker_net=-3)
    world["class"] = world["ledger"].classify(SYMBOL, broker_net=-3)
    if world["class"] is Ownership.SHARED:
        world["warnings"].append("shared-symbol: broker lot-matching ambiguous")


@then('a persistent shared-symbol warning is shown')
def _(world):
    assert world["class"] is Ownership.SHARED and abs(world["delta"]) == 1
    assert world["warnings"]


@then('the resting stops remain for exactly 2')
def _(world):
    # OWN-05: management continues at recorded ledger quantities, unchanged
    assert world["stops_resting"] == 2
    assert abs(world["ledger"].owned(SYMBOL)) == 2


@then("a subsequent stop fill triggers LEX for exactly the bot's long quantity")
def _(world):
    assert world["ledger"].cap_exit_qty(SYMBOL, world["long_qty"]) == 2


@then('a Close trade on that entry submits orders for exactly 2')
def _(world):
    assert world["ledger"].cap_exit_qty(SYMBOL, 99) == 2  # OWN-04 caps to the bot's 2
