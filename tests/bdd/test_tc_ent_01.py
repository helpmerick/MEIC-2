"""Hand-written step definitions for TC-ENT-01 — entry window + 4-leg order
(ENT-02, ORD-01/02)."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import ET, FakeClock

scenarios("../features/TC-ENT-01.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 6, 10, 0, 0, tzinfo=ET)
GATES_PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                          flatten_in_progress=False, market_open=True, market_halted=False,
                          data_fresh=True, session_valid=True, buying_power_ok=True)
CONDOR = Condor(entry_number=1, put_short=D("5990"), call_short=D("6060"),
                put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                mid_credit=D("4.00"), min_total_credit=D("2.00"))


@pytest.fixture
def world():
    return {}


def _attempt(world):
    svc = ExecuteEntryAttempt(world["broker"], world["clock"], [], SPX)
    world["outcome"] = asyncio.run(svc.attempt(
        day="2026-07-06", scheduled=SCHEDULED, condor=CONDOR, gates=GATES_PASS))


# --- Scenario 1: executes inside its window ----------------------------------

@given('the clock reaches 10:00:00 ET')
def _(world):
    world["broker"] = FakeBroker()
    world["broker"].script_submit(Scripted("fill", payload={"net_credit": "4.00"}))
    world["clock"] = FakeClock(SCHEDULED)


@when('the entry attempt begins within entry_window_seconds')
def _(world):
    _attempt(world)


@then('a 4-leg condor limit order is submitted per ORD-01/ORD-02')
def _(world):
    orders = list(world["broker"]._orders.values())
    condor_orders = [o for o in orders if len(o.intent.legs) == 4 and o.intent.order_type == "limit"]
    assert len(condor_orders) == 1
    assert world["outcome"].status == "FILLED"


# --- Scenario 2: missed window is never executed late ------------------------

@given('the bot was down from 09:55 to 10:05 ET')
def _(world):
    world["broker"] = FakeBroker()
    world["clock"] = FakeClock(datetime(2026, 7, 6, 10, 5, 0, tzinfo=ET))  # restart time


@when('the bot restarts at 10:05')
def _(world):
    _attempt(world)


@then('entry 1 is marked SKIPPED with reason "missed_window"')
def _(world):
    assert world["outcome"].status == "SKIPPED"
    assert world["outcome"].reason == "missed_window"


@then('no order for entry 1 is ever submitted')
def _(world):
    assert world["broker"]._orders == {}
