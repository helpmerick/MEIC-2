"""Paper demo runtime — drives a compressed paper day you can watch live.

This is the runtime loop the panel observes: it arms a schedule, fires entries
one by one against the SimulatedBroker (real trade-through fills), protects
each with resting stops, stops one side out mid-day (SIM-03 slippage) and
recovers its long via LEX, closes one entry by decay, and settles the rest at
EOD — appending events to the shared log in real time so the read-model
endpoints show the day unfold. It loops so there is always activity to see.

A genuine session would drive RunTradingDay against wall-clock time with the
real DXLink feed; this compresses the day to ~30 s and synthesizes fills so it
is watchable on demand, exercising the SAME services and event pipeline.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from meic.adapters.sim.simulated_broker import SimLedger
from meic.application.execute_entry import Condor
from meic.application.market_calendar import trading_day
from meic.application.protect_position import ShortLeg
from meic.application.recover_long import Quote
from meic.composition.paper import PaperComposition
from meic.domain.events import DayArmed, DayCompleted, EntryClosed, SideExpired
from meic.domain.stop_policy import StopBasis


class PaperDemoRuntime:
    def __init__(self, comp: PaperComposition, *, step_seconds: float = 3.0) -> None:
        self.comp = comp
        self.step = step_seconds
        self._entry_times = ["09:32", "10:00", "10:30", "11:00", "11:30", "12:00"]

    def _condor(self, n: int, contracts: int = 1) -> Condor:
        put_short, call_short = D(str(5990 - n)), D(str(6060 + n))
        return Condor(entry_number=n, put_short=put_short, call_short=call_short,
                      put_long=put_short - 50, call_long=call_short + 50,
                      put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                      mid_credit=D("4.00"), min_total_credit=D("2.00"),
                      expiration=trading_day(self.comp.clock.now()), contracts=contracts)

    def _reset(self) -> None:
        self.comp.events.clear()
        self.comp.broker.ledger = SimLedger(cash=D("100000"))
        self.comp.state.armed = False
        self.comp.state.stop_trading = False

    async def run_once(self) -> None:
        comp, e = self.comp, self.comp.events

        def market(intent):
            # condors trade through their 4.00 credit limit
            if intent.kind == "iron_condor":
                return (D("4.00"), D("4.10"), True)
            action = intent.legs[0].action
            if action == "sell_to_close":      # LEX long sale (credit)
                return (D("0.40"), D("0.40"), True)
            if action == "buy_to_close":       # manual/flatten close of a short (debit)
                price = intent.price or D("0.05")
                return (price, price, False)   # fills so the book actually goes flat
            return None

        comp.broker.set_market(market)
        comp.compose_and_arm(self._entry_times)
        day = "2026-07-07"
        e.append(DayArmed(date=day, entry_count=6))

        base = datetime(2026, 7, 7, 9, 30, tzinfo=timezone.utc)
        for i in range(6):
            await asyncio.sleep(self.step)
            if not comp.state.entries_enabled():   # operator may have hit Stop Trading in the UI
                continue
            n = i + 1
            if hasattr(comp.clock, "set_time"):
                comp.clock.set_time(base + timedelta(minutes=30 * i))
            condor = self._condor(n)
            outcome = await comp.execute.attempt(day=day, scheduled=comp.clock.now(),
                                                 condor=condor, gates=comp.day._gates())
            if outcome.status == "FILLED":
                await comp._on_filled(f"{day}#{n}", condor)  # STP-01 place the resting stops
            # mid-day: entry 2's put stops out (SIM-03), long LEX-recovered
            if n == 2:
                await asyncio.sleep(self.step)
                comp.broker.tick_marks({"short_put": D("3.85")}, entry_id=f"{day}#2")
                await comp.recover.recover(entry_id=f"{day}#2", side="PUT", long_symbol="SPXW_5938P",
                                           quote=Quote(bid=D("0.38"), ask=D("0.42")), intrinsic=D("0"))
            # a bit later: entry 3 closes by decay
            if n == 3:
                await asyncio.sleep(self.step)
                comp.events.append(EntryClosed(entry_id=f"{day}#3", initiator="decay",
                                                at=comp.clock.now().isoformat()))
        # EOD: remaining sides expire worthless
        await asyncio.sleep(self.step)
        for n in (1, 4, 5, 6):
            for side in ("PUT", "CALL"):
                e.append(SideExpired(entry_id=f"{day}#{n}", side=side, at=comp.clock.now().isoformat()))
        e.append(DayCompleted(date=day))

    async def run_forever(self) -> None:
        while True:
            try:
                self._reset()
                await self.run_once()
                await asyncio.sleep(self.step * 3)  # hold the finished day, then replay
            except asyncio.CancelledError:
                raise
            except Exception:  # a demo loop must never take the process down
                await asyncio.sleep(self.step)
