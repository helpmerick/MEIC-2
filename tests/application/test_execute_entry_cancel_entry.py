"""CLS-03 x the reprice ladder — the working-entry registry seam (2026-07-11).

The ladder (`ExecuteEntryAttempt._work_order`) is the ONLY code that ever
knows a pre-fill entry's broker order id: it lives in a ladder-local variable
and is journaled nowhere (CondorProposed carries strikes only). CLS-03's
panel path (PanelCommands -> ManualClose.cancel_working) therefore needs the
ladder to (a) PUBLISH its current working order id to the shared
`WorkingEntryOrders` registry while the order works, and (b) STAND DOWN when
the panel raises the cancel flag — never repricing (and, on the live
adapter's cancel-then-submit replace fallback, never RESUBMITTING) an order
the operator just cancelled. The stand-down reuses the exact cancel-and-
confirm block the floor exit already uses, so the raced-fill guard (a fill
landing inside the cancel round trip is recorded as the fill it is) applies
to the operator cancel identically.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.working_entries import WorkingEntryOrders
from meic.domain.events import CondorFilled, EntrySkipped
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
SCHEDULED = datetime(2026, 7, 11, 10, 0, tzinfo=ET)
PASS = GateSnapshot(armed=True, confirm_live=True, stop_trading=False, flatten_in_progress=False,
                    market_open=True, market_halted=False, data_fresh=True, session_valid=True,
                    buying_power_ok=True)
CONDOR = Condor(1, D("5990"), D("6060"), D("3.00"), D("2.00"), D("4.00"), D("2.00"))


async def _drive(clock, coro):
    """Advance the FakeClock so the ladder's reprice-interval waits elapse
    (same shape as tests/application/test_entry_pipeline.py's `_drive`)."""
    task = asyncio.ensure_future(coro)
    for _ in range(2000):
        if task.done():
            break
        for _ in range(6):
            await asyncio.sleep(0)
        clock.advance(seconds=5)
    return await task


class _RecordSpy(WorkingEntryOrders):
    """Observes record() calls as they happen (the registry is empty again by
    the time attempt() returns — cleared on exit — so live observation is the
    only way to prove the id was published WHILE the order worked)."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def record(self, entry_id, order_id):
        self.seen.append((entry_id, str(order_id)))
        super().record(entry_id, order_id)


class _CancelOnFirstRecord(_RecordSpy):
    """Simulates the operator's Cancel entry landing the moment the entry
    order rests — the panel raises the flag, ManualClose does the broker
    cancel; here only the flag matters (the ladder must stand down on it)."""

    def record(self, entry_id, order_id):
        super().record(entry_id, order_id)
        self.request_cancel(entry_id)


def test_ladder_publishes_the_working_order_id_and_clears_it_on_exit():
    registry = _RecordSpy()
    broker, events = FakeBroker(), []   # default: every submit rests WORKING
    clock = FakeClock(SCHEDULED)
    svc = ExecuteEntryAttempt(broker, clock, events, SPX,
                              entry_reprice_attempts=5, working_orders=registry)

    out = asyncio.run(_drive(clock, svc.attempt(
        day="2026-07-11", scheduled=SCHEDULED, condor=CONDOR, gates=PASS)))

    assert out.status == "SKIPPED" and out.reason == "unfilled_at_floor"
    assert registry.seen, "the ladder must publish its working order id (CLS-03 seam)"
    assert all(eid == "2026-07-11#1" for eid, _ in registry.seen)
    # cleared on exit: nothing left to cancel once the attempt ended
    assert registry.get("2026-07-11#1") is None
    assert not registry.cancel_requested("2026-07-11#1")


def test_operator_cancel_mid_ladder_stands_down_without_resubmitting():
    registry = _CancelOnFirstRecord()
    broker, events = FakeBroker(), []
    clock = FakeClock(SCHEDULED)
    svc = ExecuteEntryAttempt(broker, clock, events, SPX,
                              entry_reprice_attempts=5, working_orders=registry)

    out = asyncio.run(_drive(clock, svc.attempt(
        day="2026-07-11", scheduled=SCHEDULED, condor=CONDOR, gates=PASS)))

    assert out.status == "SKIPPED" and out.reason == "cancelled_by_operator"
    # exactly ONE order ever reached the broker: no reprice/replace (which on
    # the live adapter's fallback could RESUBMIT the cancelled order)
    assert len(broker._orders) == 1
    skips = [e for e in events if isinstance(e, EntrySkipped)]
    assert [s.reason for s in skips] == ["cancelled_by_operator"]
    assert registry.get("2026-07-11#1") is None   # cleared on exit


def test_operator_cancel_racing_a_fill_is_recorded_as_the_fill_it_is():
    """The order FILLS inside the stand-down's own cancel round trip: the
    post-cancel re-check (the same guard as cancel-at-floor) records the fill
    — never an EntrySkipped for a condor that is, in fact, live."""

    class _RaceBroker(FakeBroker):
        async def cancel(self, id):
            rec = self._orders.get(id)
            if rec and rec.status == "WORKING":
                self._record_fill(rec, {}, partial=False)   # the race
            return {"result": "terminal", "status": "FILLED"}

    registry = _CancelOnFirstRecord()
    broker, events = _RaceBroker(), []
    clock = FakeClock(SCHEDULED)
    svc = ExecuteEntryAttempt(broker, clock, events, SPX,
                              entry_reprice_attempts=5, working_orders=registry)

    out = asyncio.run(_drive(clock, svc.attempt(
        day="2026-07-11", scheduled=SCHEDULED, condor=CONDOR, gates=PASS)))

    assert out.status == "FILLED"
    assert sum(isinstance(e, CondorFilled) for e in events) == 1
    assert not any(isinstance(e, EntrySkipped) for e in events)


def test_a_service_with_no_registry_behaves_exactly_as_before():
    """`working_orders=None` (every pre-wiring caller, incl. paper harnesses
    that never pass one) must change nothing: the floor exit still reads
    `unfilled_at_floor`."""
    broker, events = FakeBroker(), []
    clock = FakeClock(SCHEDULED)
    svc = ExecuteEntryAttempt(broker, clock, events, SPX, entry_reprice_attempts=5)

    out = asyncio.run(_drive(clock, svc.attempt(
        day="2026-07-11", scheduled=SCHEDULED, condor=CONDOR, gates=PASS)))

    assert out.status == "SKIPPED" and out.reason == "unfilled_at_floor"
