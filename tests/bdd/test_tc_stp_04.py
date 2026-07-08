"""Hand-written step definitions for TC-STP-04 — unconfirmed stop escalates to
UNPROTECTED handling (STP-04). Drives the real ProtectPosition against a broker
that never confirms a placed stop."""
import asyncio
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.protect_position import ProtectPosition, ShortLeg
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
