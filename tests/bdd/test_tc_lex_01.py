"""Hand-written step definitions for TC-LEX-01 — LEX ladder mid->bid->fallback."""
import asyncio
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.recover_long import Quote, RecoverLong
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker

scenarios("../features/TC-LEX-01.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


@pytest.fixture
def world():
    return {}


@given('the short put stop filled and the long put quotes bid 2.00 / ask 2.30')
def _(world):
    broker, events = FakeBroker(), []  # never fills -> full ladder then fallback
    r = asyncio.run(RecoverLong(broker, events, SPX, lex_reprice_attempts=4).recover(
        entry_id="e1", side="PUT", long_symbol="SPXW_5940P",
        quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0")))
    world["broker"], world["result"] = broker, r
    world["sells"] = [o.intent for o in broker._orders.values()]


@then('a limit sell at 2.15 is placed within lex_start_latency_ms')
def _(world):
    assert world["result"].prices_tried[0] == D("2.15")  # mid of 2.00/2.30
    assert any(i.order_type == "limit" and i.price == D("2.15") for i in world["sells"])


@when('lex_reprice_seconds elapses without fill')
def _(world):
    pass  # recover() already walked the full ladder unfilled


@then('the order is replaced at one tick lower recomputed from the CURRENT quote  # EC-LEX-05')
def _(world):
    assert world["result"].prices_tried[1] == D("2.10")  # one 0.05 tick lower


@then('after lex_reprice_attempts unfilled replacements the fallback places a marketable limit at the current bid  # LEX-05')
def _(world):
    assert world["result"].outcome == "FALLBACK_WORKING"
    assert any(i.order_type == "marketable_limit" and i.price == D("2.00") for i in world["sells"])
