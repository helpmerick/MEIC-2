"""Hand-written step definitions for TC-ORD-08 — fill credit is the broker's,
never the working limit or a pre-fill estimate (ORD-09, STP-02d).

Drives the real ExecuteEntryAttempt with a FakeBroker + scripted fill_legs,
exactly like the BUG-1 tests in tests/application/test_entry_pipeline.py
(test_record_fill_uses_broker_allocated_prices_when_all_present and
test_record_fill_falls_back_to_rung_price_when_any_leg_price_is_missing).

The feature's "the STP-02d reconciliation record logs FAIL" clause names
machinery (meic.domain.allocation.reconcile) that is only ever invoked from
the live TastytradeAdapter path (adapters/tastytrade/adapter.py) -- it is not
reachable from ExecuteEntryAttempt driven by fakes. That clause is bound
directly against the allocation-domain function instead of faking a live
adapter call.
"""
import asyncio
from datetime import date, datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.domain.allocation import reconcile
from meic.domain.events import CondorFilled, FilledLeg
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import ET, FakeClock

scenarios("../features/TC-ORD-08.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 6, 10, 0, tzinfo=ET)
GATES_PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                          flatten_in_progress=False, market_open=True, market_halted=False,
                          data_fresh=True, session_valid=True, buying_power_ok=True)


@pytest.fixture
def world():
    return {}


def _run(world):
    clock = FakeClock(SCHEDULED)
    world["events"] = []
    world["outcome"] = asyncio.run(
        ExecuteEntryAttempt(world["broker"], clock, world["events"], SPX).attempt(
            day="d", scheduled=SCHEDULED, condor=world["condor"], gates=GATES_PASS))


# --- Scenario 1: net credit is the broker's actual fill, not the limit --------

@given("a 4-leg entry limit working at net credit 3.50")
def _(world):
    world["broker"] = FakeBroker()
    world["broker"].script_submit(Scripted("fill"))
    world["condor"] = Condor(1, D("7535"), D("7540"), D("1.85"), D("2.00"), D("3.50"), D("2.00"),
                             put_long=D("7510"), call_long=D("7565"), expiration=date(2026, 7, 9))


@given("the broker reports per-leg fill allocations: shorts 1.80 and 1.95, longs 0.08 and 0.07")
def _(world):
    async def fake_fill_legs(order_id):
        return (
            FilledLeg(symbol="SPXW260709P07535000", right="P", role="short", qty=1, price=D("1.80")),
            FilledLeg(symbol="SPXW260709P07510000", right="P", role="long", qty=1, price=D("0.08")),
            FilledLeg(symbol="SPXW260709C07540000", right="C", role="short", qty=1, price=D("1.95")),
            FilledLeg(symbol="SPXW260709C07565000", right="C", role="long", qty=1, price=D("0.07")),
        )
    world["broker"].fill_legs = fake_fill_legs


@when("the fill is recorded")
def _(world):
    _run(world)


@then("the entry's net credit is 3.60 (sum of allocated legs)")
def _(world):
    assert world["outcome"].status == "FILLED"
    assert world["outcome"].fill_credit == D("3.60")
    filled = [e for e in world["events"] if isinstance(e, CondorFilled)]
    assert len(filled) == 1 and filled[0].net_credit == D("3.60")


@then("never the 3.50 working limit or any pre-fill estimate")
def _(world):
    assert world["outcome"].fill_credit != D("3.50")
    assert world["outcome"].fill_credit != world["condor"].mid_credit  # the pre-fill estimate


# --- Scenario 2: missing allocations are never fabricated ---------------------

@given("the broker reports the fill without a usable per-leg allocation")
def _(world):
    # Mirrors ORD-01's default CONDOR (mid_credit 4.00): FakeBroker's default
    # fill_legs (simulated_fill_legs) always reports price=None per leg -- the
    # honest "no usable per-leg allocation" case, same as production paper/live
    # fills the broker reports without a leg allocation.
    world["broker"] = FakeBroker()
    world["broker"].script_submit(Scripted("fill", payload={"net_credit": "4.00"}))
    world["condor"] = Condor(1, D("5990"), D("6060"), D("3.00"), D("2.00"), D("4.00"), D("2.00"))


@then("the order-level fill price is used for net credit")
def _(world):
    assert world["outcome"].status == "FILLED"
    assert world["outcome"].fill_credit == D("4.00")


@then("no per-leg price is ever fabricated (ORD-09; the STP-02d reconciliation record logs FAIL)")
def _(world):
    filled = [e for e in world["events"] if isinstance(e, CondorFilled)]
    assert all(leg.price is None for leg in filled[0].legs)

    # STP-02d's allocation-reconciliation record (meic.domain.allocation.reconcile)
    # only fires from the live TastytradeAdapter path in production; it is
    # unreachable from ExecuteEntryAttempt driven by fakes. Bind the "logs FAIL"
    # clause against the domain function directly: with no usable per-leg
    # allocation, the allocated sum (0) can never agree with the real net fill,
    # so the record is a genuine FAIL -- never fabricated to PASS.
    record = reconcile([], net_fill=D("4.00"), tick=SPX.tick_for(D("4.00")))
    assert record.passed is False
    assert record.reason == "sum_mismatch"
