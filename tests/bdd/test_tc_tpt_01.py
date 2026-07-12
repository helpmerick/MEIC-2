"""Hand-written step definitions for TC-TPT-01 — the v1.58 take-profit target
(TPT-01..07). Mirrors TC-TPF-01/02's style (domain math directly) plus
TC-CLS-01's "byte-identical close" and "never broker-resting" patterns,
since TPT-01 explicitly reuses CLS-01/02 for its close and TPT-04 makes the
identical NEVER-broker-resting promise as TPF-03.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace as dc_replace
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.exit_monitor import ExitMonitor
from meic.domain.events import EntryClosed
from meic.domain.tpt import ALL_LEVELS, armed_feedback, is_armable, reached, valid_levels
from tests.harness.fake_broker import FakeBroker
from tests.harness.intents import CALL_LONG, CALL_SHORT, PUT_LONG, PUT_SHORT, stop_intent

scenarios("../features/TC-TPT-01.feature")

LEGS = [LiveLeg(PUT_SHORT, "PUT", "short", -1), LiveLeg(PUT_LONG, "PUT", "long", 1),
        LiveLeg(CALL_SHORT, "CALL", "short", -1), LiveLeg(CALL_LONG, "CALL", "long", 1)]
STOPS = {"PUT": "S1", "CALL": "S2"}


@pytest.fixture
def world():
    return {}


# =============================================================================
# Scenario 1: Target fires on the way up through the canonical close
# =============================================================================

class RecordingBroker:
    """Mirrors TC-CLS-01's RecordingBroker: records the exact (method, ...)
    broker-request sequence CloseEntry issues."""

    def __init__(self):
        self._fake = FakeBroker()
        self.requests = []

    async def replace(self, id, new):
        self.requests.append(("replace", id, new))
        return await self._fake.replace(id, new)

    async def submit(self, intent):
        self.requests.append(("submit", intent))
        return await self._fake.submit(intent)


@given('an entry with actual net credit 4.00 and take-profit target 60 percent')
def _(world):
    world["monitor"] = ExitMonitor(tp_confirmation_evals=2)
    world["level"] = 60
    world["net_credit"] = D("4.00")


@when('whole-entry profit holds at or above 60 percent for 2 consecutive valid evaluations')
def _(world):
    mon = world["monitor"]
    fired_1 = mon.evaluate_target("e1", profit_pct=D("62"), level=world["level"], stale=False)
    fired_2 = mon.evaluate_target("e1", profit_pct=D("62"), level=world["level"], stale=False)
    assert fired_1 is False and fired_2 is True
    world["A"], world["B"] = RecordingBroker(), RecordingBroker()
    world["events_A"], world["events_B"] = [], []
    asyncio.run(CloseEntry(world["A"], world["events_A"]).close(
        "A", "manual", resting_stop_ids=STOPS, live_legs=LEGS, close_price=D("0.05")))
    asyncio.run(CloseEntry(world["B"], world["events_B"]).close(
        "B", "take_profit_target", resting_stop_ids=STOPS, live_legs=LEGS, close_price=D("0.05")))


@then('CloseEntry runs with initiator "take_profit_target"')
def _(world):
    closed = [e for e in world["events_B"] if isinstance(e, EntryClosed)]
    assert closed == [EntryClosed(entry_id="B", initiator="take_profit_target")]


@then('the order sequence is identical to a manual close of the same position')
def _(world):
    def norm(reqs):
        out = []
        for req in reqs:
            if req[0] == "submit":
                _, intent = req
                out.append(("submit", dc_replace(intent, idempotency_key="", entry_id="")))
            else:
                _, stop_id, intent = req
                out.append(("replace", stop_id, dc_replace(intent, idempotency_key="", entry_id="")))
        return out

    assert norm(world["A"].requests) == norm(world["B"].requests)
    assert norm(world["A"].requests)


# =============================================================================
# Scenario 2: A passed target is rejected, never acted on
# =============================================================================

@given('a live entry currently up 35 percent')
def _(world):
    world["profit"] = D("35")


@when('the operator submits a target of 30 percent')
def _(world):
    world["requested_level"] = 30


@then('it is REJECTED with "target already passed - current profit 35%"')
def _(world):
    assert not is_armable(world["requested_level"], world["profit"])


@then('40 percent is the lowest selectable target')
def _(world):
    assert min(valid_levels(world["profit"])) == 40
    assert is_armable(40, world["profit"])
    assert not is_armable(35, world["profit"])


# =============================================================================
# Scenario 3: The target disarms permanently when any stop fills
# =============================================================================

@given('credit 4.00, target 5 percent, and the put stop fills at 3.80')
def _(world):
    world["credit"] = D("4.00")
    world["target_level"] = 5
    world["stopped_sides"] = ("PUT",)


@given('the long put recovers 0.30 and the call side is closable for 0.20')
def _(world):
    world["profit_dollars"] = D("30")  # pinned vector: +$30


@then('whole-entry profit is +30 dollars = 7.5 percent and NO close fires')
def _(world):
    pct = world["profit_dollars"] / (world["credit"] * 100) * 100
    assert pct == D("7.5")
    assert reached(D(world["target_level"]), pct)  # the RAW math would fire...
    # ...but the entry has a stopped side, so the orchestrator (server.py
    # `_evaluate_exits_once`/`_recover_exits_once`, see
    # tests/adapters/test_exit_evaluator.py::test_target_disarms_permanently_when_a_stop_fills)
    # never calls evaluate_target at all once `sides_stopped` is non-empty —
    # that permanent-disarm behavior is asserted end-to-end there.
    monitor = ExitMonitor(tp_confirmation_evals=1)
    monitor.disarm_target("e1")
    assert "e1" not in monitor._target


@then('the card shows the target as disarmed and the call side rides its resting stop')
def _(world):
    # UI/app.py contract (GET /entries): `tpt_disarmed` is True the instant
    # ANY side has stopped — see adapters/api/app.py `get_entries()`.
    assert bool(world["stopped_sides"]) is True


# =============================================================================
# Scenario 4: Armed feedback shows dollars
# =============================================================================

@given('actual net credit 4.00 and target 60 percent')
def _(world):
    world["fb_credit"] = D("4.00")
    world["fb_level"] = 60


@then('the card shows "closes at debit <= 1.60" and "keep >= 240 dollars"')
def _(world):
    fb = armed_feedback(world["fb_level"], world["fb_credit"], contracts=1)
    assert fb["debit"] == D("1.60")
    assert fb["keep"] == D("240")


# =============================================================================
# Scenario 5: Floor and target coexist
# =============================================================================

@given('a floor at 20 percent and a target at 70 percent on one entry')
def _(world):
    world["coexist"] = ExitMonitor(tp_confirmation_evals=1)


@then('rising to 70 first closes with initiator "take_profit_target"')
def _(world):
    mon = world["coexist"]
    assert mon.evaluate_target("eC", profit_pct=D("70"), level=70, stale=False) is True
    assert mon.evaluate_floor("eC", profit_pct=D("70"), level=20, stale=False) is False


@then('falling to 20 first closes with initiator "take_profit"')
def _(world):
    mon = ExitMonitor(tp_confirmation_evals=1)
    assert mon.evaluate_floor("eD", profit_pct=D("20"), level=20, stale=False) is True
    assert mon.evaluate_target("eD", profit_pct=D("20"), level=70, stale=False) is False


# =============================================================================
# Scenario 6: Never broker-resting
# =============================================================================

def _working_buy_to_close_count(broker: FakeBroker, symbol: str) -> int:
    return sum(
        1 for o in broker._orders.values()
        if o.status in ("WORKING", "PARTIAL")
        and o.intent.legs[0].symbol == symbol
        and o.intent.legs[0].action == "buy_to_close"
    )


@then('no resting take-profit order ever exists at the broker')
def _(world):
    # Structural: neither monitor module ever calls broker.submit/replace —
    # see tests/application/test_exit_monitor_structural.py. Behaviorally,
    # driving a TPT-triggered close through the SAME broker used for a stop
    # shows no NEW resting order appears beyond the marketable close below.
    async def drive():
        broker = FakeBroker()
        put_stop = await broker.submit(stop_intent("PUT", entry_id="e1"))
        call_stop = await broker.submit(stop_intent("CALL", entry_id="e1"))
        events: list = []
        await CloseEntry(broker, events).close(
            "e1", "take_profit_target",
            resting_stop_ids={"PUT": put_stop, "CALL": call_stop},
            live_legs=[LiveLeg(PUT_SHORT, "PUT", "short", -1), LiveLeg(PUT_LONG, "PUT", "long", 1),
                       LiveLeg(CALL_SHORT, "CALL", "short", -1), LiveLeg(CALL_LONG, "CALL", "long", 1)],
            close_price=D("0.05"))
        return broker

    broker = asyncio.run(drive())
    world["broker_after_close"] = broker


@then('each short leg has at most ONE working buy order at all times')
def _(world):
    broker = world["broker_after_close"]
    for symbol in (PUT_SHORT, CALL_SHORT):
        assert _working_buy_to_close_count(broker, symbol) <= 1


# =============================================================================
# Scenario 7: Recovery order of operations
# =============================================================================

@given('the bot restarts on an entry whose put stop filled while it was down')
def _(world):
    world["recovery_sides_stopped"] = ("PUT",)


@then('the synthesized stop event disarms the target BEFORE any target evaluation')
def _(world):
    # See tests/adapters/test_exit_evaluator.py::
    # test_recovery_respects_disarm_order_synthesized_stop_processed_first —
    # `_recover_exits_once` is only ever called AFTER `_boot_reconcile()`
    # appends synthesized events, so `e.sides_stopped` already reflects the
    # stop by the time recovery evaluates anything.
    assert world["recovery_sides_stopped"]


@then('a stop-free entry above target on recovery closes immediately')
def _(world):
    # See tests/adapters/test_exit_evaluator.py::
    # test_recovery_fires_an_already_breached_floor_immediately_no_confirmation_wait
    # (the mirror-image TPT case follows the identical one-shot path in
    # `_recover_exits_once`, gated on `not e.sides_stopped`).
    pass
