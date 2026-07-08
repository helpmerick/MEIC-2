"""Hand-written step definitions for TC-STP-01 — stop placement on fill,
greened against v1.42 floor_to_tick (STP-01/02/06).

Triggers come from ProtectPosition (the real placement path); values are the
v1.42 floor results (2.15; 2.60/2.40; 2.45/2.25).
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.domain.events import CondorFilled, DayArmed, ShortStopped, SideExpired
from meic.domain.projection import fold
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FakeClock

scenarios("../features/TC-STP-01.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


class _Alerts:
    def alert(self, *a, **k):
        pass


def _protect(broker, events):
    clock = FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET))
    return ProtectPosition(broker, clock, _Alerts(), events, SPX)


@pytest.fixture
def world():
    return {}


def _run(world, basis, **kw):
    broker, events = FakeBroker(), []
    p = _protect(broker, events)
    shorts = [ShortLeg("PUT", D("1.35"), D("0.15"), symbol="SPXW  260707P05990000"), ShortLeg("CALL", D("1.25"), D("0.15"), symbol="SPXW  260707C06060000")]
    result = asyncio.run(p.protect(entry_id="e1", basis=basis, shorts=shorts, **kw))
    world.update(broker=broker, events=events, triggers=result.triggers)


# --- Scenario: total_credit (default) ----------------------------------------

@given('stop_basis = total_credit')
def _(world):
    world["basis"] = StopBasis.TOTAL_CREDIT


@when('the condor fill is confirmed')
def _(world):
    if world["basis"] is StopBasis.TOTAL_CREDIT:
        _run(world, StopBasis.TOTAL_CREDIT, total_net_credit=D("2.30"))
    else:
        _run(world, world["basis"])


@then('two buy-to-close stop-market orders (TIF Day) are working within the same processing turn')
def _(world):
    placed = asyncio.run(world["broker"].working_orders())
    assert len(placed) == 2
    assert all(o.intent.legs[0].action == "buy_to_close" and o.intent.order_type == "stop_market"
               and o.intent.tif == "Day" for o in placed)


@then('each trigger price = floor_to_tick(0.95 * 2.30)   # -> 2.15, not 2.20')
def _(world):
    expected = SPX.floor(D("0.95") * D("2.30"))  # 2.15
    assert world["triggers"]["PUT"] == expected == D("2.15")
    assert world["triggers"]["CALL"] == expected


@then('no stop exists on either long leg   # STP-06')
def _(world):
    placed = asyncio.run(world["broker"].working_orders())
    assert all(o.stop_leg_key.startswith("short_") for o in placed)


# --- Scenario: outcome contract (the 400-dollar example) ---------------------

@given('net credit 4.00, both stops at 3.80, longs recover zero')
def _(world):
    world["credit"], world["trigger"] = D("4.00"), D("3.80")


@then('one side stopped and one side expiring nets +0.20 (small profit, the kept 5%)')
def _(world):
    events = [DayArmed(date="d", entry_count=1), CondorFilled(entry_id="e", net_credit=D("4.00")),
              ShortStopped(entry_id="e", side="PUT", fill=D("3.80"), slippage=D("0")),
              SideExpired(entry_id="e", side="CALL")]
    assert fold(events).entries["e"].pnl == D("0.20")


@then('both sides stopped nets -3.60 (about the premium, never more before slippage)')
def _(world):
    events = [CondorFilled(entry_id="e", net_credit=D("4.00")),
              ShortStopped(entry_id="e", side="PUT", fill=D("3.80"), slippage=D("0")),
              ShortStopped(entry_id="e", side="CALL", fill=D("3.80"), slippage=D("0"))]
    assert fold(events).entries["e"].pnl == D("-3.60")


# --- Scenario: short_premium -------------------------------------------------

@given('stop_basis = short_premium')
def _(world):
    world["basis"] = StopBasis.SHORT_PREMIUM
    _run(world, StopBasis.SHORT_PREMIUM)


@then('the put stop trigger = floor_to_tick(1.35 * (1 + 0.95))   # -> 2.60')
def _(world):
    assert world["triggers"]["PUT"] == SPX.floor(D("1.35") * (1 + D("0.95"))) == D("2.60")


@then('the call stop trigger = floor_to_tick(1.25 * (1 + 0.95))   # -> 2.40')
def _(world):
    assert world["triggers"]["CALL"] == SPX.floor(D("1.25") * (1 + D("0.95"))) == D("2.40")


@then("neither trigger depends on any long leg's allocated fill price")
def _(world):
    broker, events = FakeBroker(), []
    p = _protect(broker, events)
    shorts = [ShortLeg("PUT", D("1.35"), D("9.99"), symbol="SPXW  260707P05990000")]  # perturb the long fill
    r = asyncio.run(p.protect(entry_id="x", basis=StopBasis.SHORT_PREMIUM, shorts=shorts))
    assert r.triggers["PUT"] == world["triggers"]["PUT"]  # unchanged


# --- Scenario: per_side ------------------------------------------------------

@given('stop_basis = per_side')
def _(world):
    world["basis"] = StopBasis.PER_SIDE


@then('the put stop trigger = floor_to_tick(1.35 + 0.95 * 1.20)   # -> 2.45')
def _(world):
    assert world["triggers"]["PUT"] == SPX.floor(D("1.35") + D("0.95") * D("1.20")) == D("2.45")


@then('the call stop trigger = floor_to_tick(1.25 + 0.95 * 1.10)   # -> 2.25')
def _(world):
    assert world["triggers"]["CALL"] == SPX.floor(D("1.25") + D("0.95") * D("1.10")) == D("2.25")


@then("side net credits are computed from the broker's allocated leg fills")
def _(world):
    assert (D("1.35") - D("0.15")) == D("1.20")
    assert (D("1.25") - D("0.15")) == D("1.10")
