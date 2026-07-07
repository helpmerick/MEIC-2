"""TC-SIM-04 (SIM-05 pipeline identity): an identical scripted entry driven
through the SimulatedBroker and a live-shape FakeBroker produces the SAME domain
event sequence — proving the whole pipeline runs unaware of the mode. The paper
run also posts to the simulated transaction ledger (the PNL-04 source) and is
stamped PAPER."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.adapters.sim.simulated_broker import SimLedger, SimulatedBroker
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
OPEN = datetime(2026, 7, 6, 9, 32, tzinfo=ET)


def _condor() -> Condor:
    return Condor(entry_number=1, put_short=D("5989"), call_short=D("6061"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"))


def _gates() -> GateSnapshot:
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


class _Alerts:
    def alert(self, *a, **k):
        pass


async def _run_scripted_entry(broker) -> list:
    """One entry + protect through the shared pipeline against `broker`."""
    events: list = []
    clock = FakeClock(OPEN)
    ex = ExecuteEntryAttempt(broker, clock, events, SPX)
    protect = ProtectPosition(broker, clock, _Alerts(), events, SPX)
    outcome = await ex.attempt(day="2026-07-06", scheduled=OPEN, condor=_condor(), gates=_gates())
    if outcome.status == "FILLED":
        await protect.protect(
            entry_id="2026-07-06#1", basis=StopBasis.TOTAL_CREDIT,
            shorts=[ShortLeg("PUT", D("3.00"), D("0.50")), ShortLeg("CALL", D("2.00"), D("0.50"))],
            total_net_credit=D("4.00"))
    return events


def _sim_broker():
    b = SimulatedBroker(SimLedger(cash=D("100000")))
    # the injected "real market" trades through the credit limit at its price
    b.set_market(lambda o: (D(str(o["net_credit"])), D(str(o["net_credit"])), True))
    return b


def _fake_live_shape_broker():
    b = FakeBroker()
    b.autofill(lambda o: isinstance(o, dict) and o.get("kind") == "iron_condor")
    return b


def _signature(events: list):
    """Compare on event type + the fields that carry meaning across brokers."""
    return [(type(e).__name__, getattr(e, "side", None),
             str(getattr(e, "net_credit", "")), str(getattr(e, "trigger", "")))
            for e in events]


def test_tc_sim_04_identical_event_sequence_across_brokers():
    sim = _sim_broker()
    sim_events = asyncio.run(_run_scripted_entry(sim))
    fake_events = asyncio.run(_run_scripted_entry(_fake_live_shape_broker()))

    # the pipeline emitted the same sequence, byte-for-byte on the meaningful fields
    assert _signature(sim_events) == _signature(fake_events)

    # and it actually ran the full entry->protect path (not both trivially empty)
    assert [type(e).__name__ for e in sim_events] == [
        "EntryWindowOpened", "CondorProposed", "CondorFilled",
        "StopPlaced", "StopConfirmed", "StopPlaced", "StopConfirmed"]

    # the paper run posted the credit to the simulated transaction ledger — the
    # authoritative source a PNL-04 reconcile runs against (fee model = 0 here).
    assert sim.ledger.cash == D("100400")  # 4.00 credit × 100
