"""ProtectPosition — STP-01/02/04/06 placement, verification, escalation."""
import asyncio
from decimal import Decimal as D

import pytest

from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.domain.events import EntryClosedInfeasible, SideUnprotected, StopConfirmed, StopPlaced
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import ET, FakeClock

from datetime import datetime

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


class RecordingAlerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def _protect(broker, events, alerts, **kw):
    clock = FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET))
    return ProtectPosition(broker, clock, alerts, events, SPX, **kw)


def test_total_credit_places_two_stops_on_shorts_only():
    """STP-01/06: two buy-to-close stop-market (TIF Day) on the shorts, none on longs."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    p = _protect(broker, events, alerts)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50")), ShortLeg("CALL", D("2.00"), D("0.50"))]
    result = asyncio.run(p.protect(entry_id="e1", basis=StopBasis.TOTAL_CREDIT,
                                   shorts=shorts, pct=D("95"), total_net_credit=D("4.00")))
    assert result.outcome == "PROTECTED"
    assert result.triggers == {"PUT": D("3.80"), "CALL": D("3.80")}  # shared level
    placed = asyncio.run(broker.working_orders())
    assert len(placed) == 2
    assert all(o.intent["action"] == "buy_to_close" and o.intent["type"] == "stop_market"
               and o.intent["tif"] == "Day" for o in placed)
    assert all(o.intent["leg"].startswith("short_") for o in placed)  # STP-06
    assert sum(isinstance(e, StopConfirmed) for e in events) == 2


def test_post_fill_infeasible_closes_instead_of_placing_suicidal_stop():
    """STP-02c checkpoint 2: trigger below a short's fill -> close via CLS
    (initiator infeasible_stop), never place a stop that fires at birth."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    closed = []

    async def close_cb(entry_id, initiator):
        closed.append((entry_id, initiator))

    p = _protect(broker, events, alerts, close_entry=close_cb)
    # net credit 2.00 @ 95 -> trigger 1.90, below the 3.00 short
    shorts = [ShortLeg("PUT", D("3.00"), D("1.50")), ShortLeg("CALL", D("2.00"), D("1.50"))]
    result = asyncio.run(p.protect(entry_id="e2", basis=StopBasis.TOTAL_CREDIT,
                                   shorts=shorts, pct=D("95"), total_net_credit=D("2.00")))
    assert result.outcome == "INFEASIBLE_CLOSED"
    assert closed == [("e2", "infeasible_stop")]
    assert any(isinstance(e, EntryClosedInfeasible) for e in events)
    assert asyncio.run(broker.working_orders()) == []  # no stop was placed


def test_unprotected_escalation_after_retries_exhausted():
    """STP-04: broker rejects every placement -> UNPROTECTED, flatten + alert."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    broker.script_submit(*[Scripted("reject", payload={"reason": "x"}) for _ in range(6)])
    flattened = []

    async def close_cb(entry_id, initiator):
        flattened.append((entry_id, initiator))

    p = _protect(broker, events, alerts, stop_retry_attempts=3, close_entry=close_cb)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"))]
    result = asyncio.run(p.protect(entry_id="e3", basis=StopBasis.TOTAL_CREDIT,
                                   shorts=shorts, pct=D("95"), total_net_credit=D("4.00")))
    assert result.outcome == "UNPROTECTED_FLATTENED"
    assert any(isinstance(e, SideUnprotected) for e in events)
    assert any(level == "critical" for level, _, _ in alerts.calls)
    assert flattened == [("e3", "unprotected")]


def test_short_premium_basis_per_side_triggers():
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    p = _protect(broker, events, alerts)
    # short_premium: put 3.00 * 1.95 = 5.85 floor -> 5.80 (0.10 tick)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"))]
    result = asyncio.run(p.protect(entry_id="e4", basis=StopBasis.SHORT_PREMIUM,
                                   shorts=shorts, pct=D("95")))
    assert result.triggers["PUT"] == D("5.80")  # floor(5.85) in 0.10 regime
