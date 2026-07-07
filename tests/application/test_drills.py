"""UC-12 stop-independence drill (STP-05 core claim, paper honesty per SIM-06)."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.drills import run_stop_independence_drill
from meic.composition.paper import PaperComposition
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


def _comp():
    return PaperComposition(clock=FakeClock(datetime(2026, 7, 7, 9, 30, tzinfo=ET)), ticks=SPX)


def test_drill_shows_stops_survived_with_unbroken_timestamps():
    comp = _comp()
    # two resting stops in place (as ProtectPosition would leave them)
    asyncio.run(comp.broker.submit({"type": "stop_market", "entry_id": "e1", "leg": "short_put", "trigger": "3.80"}))
    asyncio.run(comp.broker.submit({"type": "stop_market", "entry_id": "e1", "leg": "short_call", "trigger": "3.60"}))

    ev = asyncio.run(run_stop_independence_drill(comp.broker, outage_seconds=0))

    assert len(ev.stops_before) == 2 and len(ev.stops_after) == 2
    assert ev.survived is True                     # every stop still working after the outage
    assert ev.timestamps_unbroken is True          # placement times unchanged
    assert all(s["received_at"] for s in ev.stops_before)  # timestamps were recorded
    assert "SIM-06" in ev.honesty_note and "TC-STP-08" in ev.honesty_note  # honest caveat


def test_drill_survived_false_when_no_resting_stops():
    comp = _comp()
    ev = asyncio.run(run_stop_independence_drill(comp.broker, outage_seconds=0))
    assert ev.stops_before == [] and ev.survived is False  # nothing to prove


def test_drill_detects_a_stop_that_vanished_during_the_outage():
    comp = _comp()
    oid = asyncio.run(comp.broker.submit(
        {"type": "stop_market", "entry_id": "e1", "leg": "short_put", "trigger": "3.80"}))
    # a broker-side disappearance mid-outage would break the independence claim
    asyncio.run(comp.broker.cancel(oid))
    ev = asyncio.run(run_stop_independence_drill(comp.broker, outage_seconds=0))
    assert ev.survived is False
