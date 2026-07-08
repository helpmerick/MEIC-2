"""Hand-written step definitions for TC-STP-04 — unconfirmed stop escalates to
UNPROTECTED handling (STP-04). Drives the real ProtectPosition against a broker
that never confirms a placed stop."""
import asyncio
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from dataclasses import replace
from types import SimpleNamespace

from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.application.reconcile import Reconcile, TrackedShort
from meic.domain.events import SideUnprotected
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock
from datetime import datetime

scenarios("../features/TC-STP-04.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
RETRY_ATTEMPTS = 3
RETRY_SECONDS = 5


class NeverConfirmsBroker:
    """Accepts a stop submit (returns an id) but the order never appears in
    working_orders — the bot can never confirm it (STP-04)."""

    def __init__(self):
        self.submit_count = 0

    async def submit(self, order):
        self.submit_count += 1
        return f"stop-{self.submit_count}"

    async def working_orders(self):
        return []  # nothing ever confirms

    async def cancel(self, id):
        return {"result": "cancelled"}


class RecordingAlerts:
    def __init__(self):
        self.alerts = []

    def alert(self, level, message, **ctx):
        self.alerts.append((level, message, ctx))


@pytest.fixture
def world():
    return {}


@given('the broker rejects stop placement stop_retry_attempts times')
def _(world):
    world["broker"] = NeverConfirmsBroker()
    world["alerts"] = RecordingAlerts()
    world["events"] = []
    world["closes"] = []

    async def close(entry_id, initiator):
        world["closes"].append((entry_id, initiator))

    protect = ProtectPosition(
        world["broker"], FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET)),
        world["alerts"], world["events"], SPX,
        stop_retry_seconds=RETRY_SECONDS, stop_retry_attempts=RETRY_ATTEMPTS,
        unprotected_action="flatten_side", close_entry=close)
    world["result"] = asyncio.run(protect.protect(
        entry_id="e1", basis=StopBasis.TOTAL_CREDIT,
        shorts=[ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000"), ShortLeg("CALL", D("2.00"), D("0.50"), symbol="SPXW  260707C06060000")],
        total_net_credit=D("4.00")))


@then('the affected side is flattened per unprotected_action')
def _(world):
    assert world["result"].outcome == "UNPROTECTED_FLATTENED"
    unprot = [e for e in world["events"] if isinstance(e, SideUnprotected)]
    assert unprot and unprot[0].action == "flatten_side"
    assert world["closes"] == [("e1", "unprotected")]  # routed through CLS


@then('a critical alert is raised')
def _(world):
    assert any(level == "critical" for level, _, _ in world["alerts"].alerts)


@then('total unprotected time <= stop_retry_seconds * stop_retry_attempts')
def _(world):
    # each attempt waits at most stop_retry_seconds; the loop is bounded to
    # stop_retry_attempts, so unprotected time <= seconds * attempts.
    assert world["broker"].submit_count == RETRY_ATTEMPTS


# --- STP-01 quantity invariant (v1.45, operator-ratified spec law) -------------
# "each stop's quantity MUST equal its short leg's filled quantity ... a stop
# protecting less than the position is silent nakedness."

class WrongQtyBroker:
    """Confirms the stop as WORKING — but at quantity 1, protecting half of a
    2-contract short. The bot must notice, and must NOT resize it."""

    def __init__(self, working_qty: int = 1):
        self.working_qty = working_qty
        self.submitted: list = []
        self.resizes: list = []

    async def submit(self, order):
        self.submitted.append(order)
        return f"stop-{len(self.submitted)}"

    async def working_orders(self):
        # a working order whose intent carries the WRONG size
        undersized = replace(
            self.submitted[-1], contracts=self.working_qty,
            legs=tuple(replace(leg, qty=self.working_qty) for leg in self.submitted[-1].legs))
        return [SimpleNamespace(order_id=f"stop-{len(self.submitted)}", intent=undersized)]

    async def replace(self, id, new):        # a silent resize would land here
        self.resizes.append((id, new))
        return "resized"

    async def cancel(self, id):
        return {"result": "cancelled"}


@given('an entry filled with contracts = 2')
def _(world):
    world["contracts"] = 2


@when('a stop is confirmed working with quantity 1')
def _(world):
    world["broker"] = WrongQtyBroker(working_qty=1)
    world["alerts"] = RecordingAlerts()
    world["events"] = []
    world["closes"] = []

    async def close(entry_id, initiator):
        world["closes"].append((entry_id, initiator))

    protect = ProtectPosition(
        world["broker"], FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET)),
        world["alerts"], world["events"], SPX,
        stop_retry_seconds=RETRY_SECONDS, stop_retry_attempts=RETRY_ATTEMPTS,
        unprotected_action="flatten_side", close_entry=close)
    world["result"] = asyncio.run(protect.protect(
        entry_id="e1", basis=StopBasis.TOTAL_CREDIT,
        shorts=[ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000")],
        total_net_credit=D("4.00"), contracts=world["contracts"]))


@then('the mismatch is detected at placement confirmation')
def _(world):
    # exactly one placement attempt: a wrong-size confirmation is not retried
    # (a retry would rest a SECOND stop beside the undersized one)
    assert len(world["broker"].submitted) == 1
    assert all(leg.qty == 2 for leg in world["broker"].submitted[0].legs)  # we asked for 2


@then('the condition is handled as UNPROTECTED per STP-04')
def _(world):
    assert world["result"].outcome == "UNPROTECTED_FLATTENED"
    unprot = [e for e in world["events"] if isinstance(e, SideUnprotected)]
    assert unprot and unprot[0].action == "flatten_side"
    assert world["closes"] == [("e1", "unprotected")]
    assert world["broker"].resizes == []          # the bot never silently resizes


@then('a critical alert names the naked quantity')
def _(world):
    critical = [(m, ctx) for lvl, m, ctx in world["alerts"].alerts if lvl == "critical"]
    assert critical, "no critical alert"
    msg, ctx = critical[0]
    assert ctx["naked_quantity"] == "1"           # 2 short, 1 protected
    assert "naked" in msg


# --- the same invariant, discovered later, by reconcile ------------------------

@given("a working stop whose quantity no longer equals the short leg's ledger quantity")
def _(world):
    world["tracked"] = TrackedShort("e1", "PUT", "SPXW_5990P", stop_order_id="stop-1",
                                    stop_filled=False, stop_trigger=D("3.80"), contracts=2)
    world["working_qty"] = {"stop-1": 1}          # broker truth: only 1 protected


@when('reconcile runs')
def _(world):
    world["events"] = []
    world["plan"] = Reconcile(WrongQtyBroker(), world["events"]).plan(
        tracked_shorts=[world["tracked"]],
        broker_working_order_ids={"stop-1"},
        mid_lex_sides=[], stale_entry_order_ids=[],
        working_stop_quantities=world["working_qty"])


@then('the entry is treated as UNPROTECTED (or OWN-10 if operator-resized)')
def _(world):
    plan = world["plan"]
    assert plan.quantity_mismatches == [("e1", "PUT", 1, 2)]
    assert plan.blocks_entries is True            # RSK-03: an unresolved mismatch
    assert "1 naked" in plan.mismatches[0]

    # OWN-10/11: the same mismatch, but the OPERATOR resized it -> stand down
    operator = replace(world["tracked"], stop_resized_by_operator=True)
    own10 = Reconcile(WrongQtyBroker(), []).plan(
        tracked_shorts=[operator], broker_working_order_ids={"stop-1"},
        mid_lex_sides=[], stale_entry_order_ids=[],
        working_stop_quantities=world["working_qty"])
    assert own10.user_unprotected == [("e1", "PUT")]
    assert own10.quantity_mismatches == []        # never auto-corrected


@then('the bot never silently resizes the stop itself')
def _(world):
    plan = world["plan"]
    # no re-place, no replace, no new stop order of any kind
    assert plan.place_stops == [] and plan.stop_specs == {}
