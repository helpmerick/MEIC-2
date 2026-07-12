"""RunTradingDay unit tests + the full scripted-day capstone (dress rehearsal
for paper-mode E2E): compose 6 entries, arm, run open->close under FakeClock
with a mid-day stop-out and a decay buyback, assert the day report.
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.decay_watcher import DecayWatcher
from meic.application.persistent_state import PersistentState
from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.application.recover_long import Quote, RecoverLong
from meic.application.run_trading_day import RunTradingDay, ScheduledEntry
from meic.domain.events import SideExpired
from meic.domain.stop_policy import StopBasis
from meic.domain.projection import day_report
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
OPEN = datetime(2026, 7, 6, 9, 30, tzinfo=ET)


def _armed_state(n_entries: int) -> PersistentState:
    s = PersistentState(InMemoryStateStore())
    s.entry_schedule = [{"time": f"1{i}:00"} for i in range(n_entries)]
    s.armed = True
    s.confirm_live = True
    return s


def _condor(n: int) -> Condor:
    # net credit 4.00, shorts 3.00/2.00 -> trigger 3.80 feasible (STP-02c)
    return Condor(entry_number=n, put_short=D(str(5990 - n)), call_short=D(str(6060 + n)),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"))


def _schedule(n: int, *, same_time: bool = False) -> list[ScheduledEntry]:
    step = 0 if same_time else 30
    return [ScheduledEntry(OPEN + timedelta(minutes=step * i), _condor(i + 1)) for i in range(n)]


IS_CONDOR = lambda o: o.kind == "iron_condor"


class _Alerts:
    def alert(self, *a, **k):
        pass


def test_run_day_fires_only_enabled_entries():
    """ENT-01a: DISARMED (Confirm Live off) fires nothing; the blocking state
    is named on each skip."""
    broker, events = FakeBroker(), []
    state = _armed_state(3)
    state.confirm_live = False  # -> entries blocked
    clock = FakeClock(OPEN)
    day = RunTradingDay(clock, state, ExecuteEntryAttempt(broker, clock, events, SPX), events)
    filled = asyncio.run(day.run("2026-07-06", _schedule(3, same_time=True)))
    assert filled == 0
    rpt = day_report(events)
    assert rpt.entries_filled == 0
    assert all(reason == "CONFIRM_LIVE_OFF" for _, reason in rpt.skips)


def test_run_day_respects_max_entries():
    broker, events = FakeBroker(), []
    broker.autofill(IS_CONDOR)
    state = _armed_state(4)
    clock = FakeClock(OPEN)
    day = RunTradingDay(clock, state, ExecuteEntryAttempt(broker, clock, events, SPX),
                        events, max_entries_per_day=2)
    filled = asyncio.run(day.run("2026-07-06", _schedule(4, same_time=True)))
    assert filled == 2
    assert ("max_entries" in {r for _, r in day_report(events).skips})


def test_full_scripted_day_capstone():
    """Compose 6 entries, arm, run open->close: all 6 fill and get stops; entry
    2's put stops out mid-day and its long is recovered via LEX; entry 3 is
    closed by a decay buyback; the rest expire at EOD. Assert the day report."""
    broker, events = FakeBroker(), []
    broker.autofill(IS_CONDOR)  # entry orders fill; stops rest WORKING
    clock = FakeClock(OPEN)
    state = _armed_state(6)

    protect = ProtectPosition(broker, clock, _Alerts(), events, SPX)

    async def on_filled(entry_id, condor):
        # STP-01 hand-off: place the two resting stops (total_credit @95 -> 3.80)
        await protect.protect(
            entry_id=entry_id, basis=StopBasis.TOTAL_CREDIT,
            shorts=[ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000"), ShortLeg("CALL", D("2.00"), D("0.50"), symbol="SPXW  260707C06060000")],
            total_net_credit=D("4.00"))

    day = RunTradingDay(clock, state, ExecuteEntryAttempt(broker, clock, events, SPX),
                        events, on_filled=on_filled)

    async def scenario():
        run = asyncio.create_task(day.run("2026-07-06", _schedule(6)))
        # advance the clock across the day so each entry window opens in turn
        for i in range(6):
            clock.set_time(OPEN + timedelta(minutes=30 * i))
            for _ in range(4):
                await asyncio.sleep(0)  # let run() fire the entry + place stops
        clock.set_time(OPEN + timedelta(hours=6, minutes=30))  # near close
        await run

    asyncio.run(scenario())

    # --- mid-day reactive management (process managers reacting to the log) ---
    from meic.domain.events import ShortStopped
    # entry 2 put stops out at 3.80, then LEX recovers the long for 0.40
    events.append(ShortStopped(entry_id="2026-07-06#2", side="PUT", fill=D("3.80"), slippage=D("0.05")))
    broker.script_submit(Scripted("fill", payload={"price": "0.40"}))  # the LEX sell fills
    asyncio.run(RecoverLong(broker, clock, events, SPX).recover(
        entry_id="2026-07-06#2", side="PUT", long_symbol="SPXW_5938P",
        quote=Quote(bid=D("0.38"), ask=D("0.42")), intrinsic=D("0")))  # mid 0.40
    # entry 3 decayed: ask <= 0.05 x2 -> buyback close, initiator decay
    dcy = DecayWatcher(broker, events)
    assert dcy.evaluate(ask=D("0.05")) is False and dcy.evaluate(ask=D("0.05")) is True
    asyncio.run(dcy.complete(entry_id="2026-07-06#3", side="CALL"))
    # EOD: every other entry's sides expire worthless (EOD-01)
    for n in (1, 4, 5, 6):
        for side in ("PUT", "CALL"):
            events.append(SideExpired(entry_id=f"2026-07-06#{n}", side=side))

    # --- the day report -------------------------------------------------------
    rpt = day_report(events)
    assert rpt.entries_filled == 6            # all six composed entries filled
    assert rpt.stops_hit == 1                 # entry 2's put
    assert rpt.lex_recoveries == 1            # its long recovered via LEX
    assert rpt.decay_closes == 1              # entry 3 bought back
    assert rpt.total_credit == D("24.00")     # 6 x 4.00
    assert rpt.skips == ()                    # nothing skipped
    # P&L sanity: 6 entries banked 4.00 each; entry 2 paid 3.80 to stop, recovered
    # ~0.40 on the long; entry 3 paid 0.05 to decay-close. Positive day.
    assert rpt.day_pnl > 0
    assert rpt.per_entry_pnl["2026-07-06#2"] == D("4.00") - D("3.80") + D("0.40")  # 0.60
