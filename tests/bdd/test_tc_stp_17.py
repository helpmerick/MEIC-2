"""Hand-written step definitions for TC-STP-17 — the STP-03b stop watchdog."""
import asyncio
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.watchdog import StopWatchdog
from meic.domain.events import ShortStopped, WatchdogEscalated
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.intents import condor_intent, stop_intent

scenarios("../features/TC-STP-17.feature")

TRIGGER = D("3.80")
ENTRY, SIDE = "e1", "PUT"


class RecordingAlerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def _watchdog():
    return StopWatchdog(broker=FakeBroker(), alerts=RecordingAlerts(), events=[])


@pytest.fixture
def world():
    return {"actions": []}


# --- Scenario 1: silent in the normal world ----------------------------------

@given('the mark crosses the trigger and the broker stop fills within 6 seconds')
def _(world):
    wd = _watchdog()
    # 6s of breach, then the resting stop fills — watchdog stays silent
    world["actions"].append(wd.observe(entry_id=ENTRY, side=SIDE, mark=D("3.85"), trigger=TRIGGER,
                                       seconds_since_last=D("6"), stop_filled=False, stale=False))
    world["actions"].append(wd.observe(entry_id=ENTRY, side=SIDE, mark=D("3.85"), trigger=TRIGGER,
                                       seconds_since_last=D("0"), stop_filled=True, stale=False))
    world["wd"] = wd


@then('the watchdog never alerts and never acts')
def _(world):
    assert all(a is None for a in world["actions"])
    assert world["wd"].alerts.calls == []
    assert world["wd"].events == []


# --- Scenario 2: alert at grace, escalate at bound ---------------------------

@given('the mark holds at or above trigger and the resting stop stays unfilled')
def _(world):
    broker = FakeBroker()
    wd = StopWatchdog(broker=broker, alerts=RecordingAlerts(), events=[])
    # place the resting stop (stays WORKING/unfilled)
    resting_id = asyncio.run(broker.submit(stop_intent("PUT")))
    wd.resting_stop_ids[(ENTRY, SIDE)] = resting_id
    # 10s breach -> alert; another 10s (=20 total) -> escalate
    world["at10"] = wd.observe(entry_id=ENTRY, side=SIDE, mark=D("3.90"), trigger=TRIGGER,
                               seconds_since_last=D("10"), stop_filled=False, stale=False)
    world["at20"] = wd.observe(entry_id=ENTRY, side=SIDE, mark=D("3.90"), trigger=TRIGGER,
                               seconds_since_last=D("10"), stop_filled=False, stale=False)
    if world["at20"] == "escalate":
        asyncio.run(wd.escalate(entry_id=ENTRY, side=SIDE, mark_at_breach=D("3.90"), ask=D("3.95"), symbol="SPXW  260707P05990000"))
    world["wd"], world["broker"], world["resting_id"] = wd, broker, resting_id


@then('a critical alert fires at 10 seconds')
def _(world):
    assert world["at10"] == "alert"
    assert any(level == "critical" for level, _, _ in world["wd"].alerts.calls)


@then('at 20 seconds the bot sends a marketable buy-to-close and cancels the resting stop')
def _(world):
    assert world["at20"] == "escalate"
    orders = list(world["broker"]._orders.values())
    marketable = [o for o in orders if o.intent.order_type == "marketable_limit"
                  and o.intent.legs[0].action == "buy_to_close"]
    assert len(marketable) == 1
    resting = world["broker"]._orders[world["resting_id"]]
    assert resting.status == "CANCELLED"


@then('the side proceeds SIDE_STOPPED into LEX with initiator watchdog_escalation')
def _(world):
    stopped = [e for e in world["wd"].events if isinstance(e, ShortStopped)]
    assert len(stopped) == 1 and stopped[0].initiator == "watchdog_escalation"


# --- Scenario 3: race — broker stop fills during escalation ------------------

@given('the resting stop fills while the escalation order is in flight')
def _(world):
    broker = FakeBroker()
    wd = StopWatchdog(broker=broker, alerts=RecordingAlerts(), events=[])
    # resting stop submitted AND already filled (not in working orders) — the
    # stop won the race
    resting_id = asyncio.run(broker.submit(stop_intent("PUT")))
    broker.script_submit()  # (no-op guard)
    broker._orders[resting_id].status = "FILLED"  # the broker stop filled
    wd.resting_stop_ids[(ENTRY, SIDE)] = resting_id
    # drive to escalation threshold, then attempt escalation
    wd.observe(entry_id=ENTRY, side=SIDE, mark=D("3.90"), trigger=TRIGGER,
               seconds_since_last=D("20"), stop_filled=False, stale=False)
    asyncio.run(wd.escalate(entry_id=ENTRY, side=SIDE, mark_at_breach=D("3.90"), ask=D("3.95"), symbol="SPXW  260707P05990000"))
    world["broker"], world["wd"] = broker, wd


@then('the escalation aborts per ORD-08 and exactly one buy-back exists (order count = 1)')
def _(world):
    # escalation submitted no marketable order; the resting stop is the single buy-back
    marketable = [o for o in world["broker"]._orders.values()
                  if o.intent.order_type == "marketable_limit"]
    assert marketable == []
    buybacks = [o for o in world["broker"]._orders.values() if o.stop_leg_key == "short_put"]
    assert len(buybacks) == 1
    assert world["wd"].events == []  # no ShortStopped/escalation recorded


# --- Scenario 4: stale marks pause the clock ---------------------------------

@given('quotes go stale mid-breach')
def _(world):
    wd = _watchdog()
    wd.observe(entry_id=ENTRY, side=SIDE, mark=D("3.90"), trigger=TRIGGER,
               seconds_since_last=D("5"), stop_filled=False, stale=False)  # 5s in
    world["stale_action"] = wd.observe(entry_id=ENTRY, side=SIDE, mark=D("3.90"), trigger=TRIGGER,
                                       seconds_since_last=D("100"), stop_filled=False, stale=True)
    # a fresh obs resumes accumulation from where it paused (5s), not 105s
    world["resume_action"] = wd.observe(entry_id=ENTRY, side=SIDE, mark=D("3.90"), trigger=TRIGGER,
                                        seconds_since_last=D("1"), stop_filled=False, stale=False)
    world["wd"] = wd


@then('the watchdog clock pauses and resumes on fresh data; no action on stale marks')
def _(world):
    assert world["stale_action"] is None  # no action while stale, despite +100s
    assert world["resume_action"] is None  # resumed at 6s, still below the 10s grace
    assert world["wd"].alerts.calls == []


# --- Scenario 5: every escalation is calibration evidence --------------------

@then('each watchdog_escalation record stores mark-at-breach, elapsed time, and fill price')
def _():
    broker = FakeBroker()
    wd = StopWatchdog(broker=broker, alerts=RecordingAlerts(), events=[])
    resting_id = asyncio.run(broker.submit(stop_intent("PUT")))
    wd.resting_stop_ids[(ENTRY, SIDE)] = resting_id
    wd.observe(entry_id=ENTRY, side=SIDE, mark=D("3.92"), trigger=TRIGGER,
               seconds_since_last=D("20"), stop_filled=False, stale=False)
    asyncio.run(wd.escalate(entry_id=ENTRY, side=SIDE, mark_at_breach=D("3.92"), ask=D("3.97"), symbol="SPXW  260707P05990000"))
    rec = [e for e in wd.events if isinstance(e, WatchdogEscalated)]
    assert len(rec) == 1
    assert rec[0].mark_at_breach == D("3.92")
    assert rec[0].elapsed_seconds == D("20")
    assert rec[0].fill_price == D("3.97")
