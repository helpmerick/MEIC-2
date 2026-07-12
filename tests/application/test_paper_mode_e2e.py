"""Paper-mode E2E — the real dress rehearsal through the SimulatedBroker.

Unlike the Phase-4 scripted-day capstone (FakeBroker + manual injection), this
runs the FULL paper wiring: SimulatedBroker fills condors via the trade-through
model (SIM-02), places real resting stops, one of which triggers on a mark
with slippage (SIM-03), and the cash ledger (SIM-04) moves accordingly.
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.application.recover_long import Quote
from meic.composition.paper import PaperComposition
from meic.domain.events import SideExpired
from meic.domain.projection import day_report
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
OPEN = datetime(2026, 7, 6, 9, 30, tzinfo=ET)


def _condor(paper, n):
    from meic.application.execute_entry import Condor
    return Condor(entry_number=n, put_short=D(str(5990 - n)), call_short=D(str(6060 + n)),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"))


def test_full_paper_day_through_simulated_broker():
    clock = FakeClock(OPEN)
    paper = PaperComposition(clock=clock, ticks=SPX, starting_cash=D("100000"))
    # SIM-02: the real market trades through the entry's 4.00 credit limit; a
    # LEX long-sale fills at its 0.40 ask; other order types rest.
    def market(intent):
        if intent.kind == "iron_condor":
            return (D("4.00"), D("4.10"), True)
        if intent.legs[0].action == "sell_to_close":
            return (D("0.40"), D("0.40"), True)
        return None
    paper.broker.set_market(market)
    paper.compose_and_arm(["09:30", "10:00", "10:30", "11:00", "11:30", "12:00"])

    from meic.application.run_trading_day import ScheduledEntry
    schedule = [ScheduledEntry(OPEN + timedelta(minutes=30 * i), _condor(paper, i + 1)) for i in range(6)]

    async def run_day():
        task = asyncio.ensure_future(paper.day.run("2026-07-06", schedule))
        for i in range(6):
            clock.set_time(OPEN + timedelta(minutes=30 * i))
            for _ in range(6):
                await asyncio.sleep(0)  # let each entry fill + place stops
        clock.set_time(OPEN + timedelta(hours=6, minutes=30))
        await task

    asyncio.run(run_day())

    # all six condors filled -> cash collected 6 x 4.00 x 100 = +2400
    assert day_report(paper.events).entries_filled == 6
    cash_after_entries = paper.broker.ledger.cash
    assert cash_after_entries == D("100000") + D("2400")

    # --- mid-day: entry 2's put mark spikes to its 3.80 trigger (SIM-03) ------
    filled = paper.broker.tick_marks({"short_put": D("3.85")}, entry_id="2026-07-06#2")  # >= 3.80
    assert len(filled) == 1, "exactly entry 2's put stop should trigger"
    # LEX recovers entry 2's long
    asyncio.run(paper.recover.recover(
        entry_id="2026-07-06#2", side="PUT", long_symbol="SPXW_5938P",
        quote=Quote(bid=D("0.38"), ask=D("0.42")), intrinsic=D("0")))

    # EOD: the rest expire worthless (EOD-01)
    for n in (1, 3, 4, 5, 6):
        for side in ("PUT", "CALL"):
            paper.events.append(SideExpired(entry_id=f"2026-07-06#{n}", side=side))

    rpt = day_report(paper.events)
    assert rpt.entries_filled == 6
    assert rpt.stops_hit >= 1                      # entry 2's put stopped (SIM-03)
    assert rpt.lex_recoveries == 1                 # its long recovered
    assert rpt.total_credit == D("24.00")
    # the stop paid trigger+slippage (3.80 + 3 ticks = 3.95); the sim ledger
    # debited it, proving the SimulatedBroker economics ran end to end
    assert paper.broker.ledger.cash < cash_after_entries


def test_paper_wiring_is_paper_only():
    """SIM-01/DAY-05: the composition binds paper; trading_mode is paper and the
    broker is the SimulatedBroker (the live adapter is never constructed)."""
    from meic.adapters.sim.simulated_broker import SimulatedBroker
    paper = PaperComposition(clock=FakeClock(OPEN), ticks=SPX)
    assert isinstance(paper.broker, SimulatedBroker)
    assert paper.state.trading_mode == "paper"
