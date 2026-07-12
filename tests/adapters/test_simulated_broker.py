"""SimulatedBroker + fill model — SIM-02/03/04 units."""
import asyncio
from decimal import Decimal as D

from meic.adapters.sim.simulated_broker import SimLedger, SimulatedBroker, spread_margin
from meic.domain.sim_fill import limit_fills, stop_fill_price, stop_triggered
from tests.harness.intents import condor_intent, stop_intent

TICK = D("0.05")


class TestFillModel:
    def test_sim02_touch_does_not_fill_trade_through_does(self):
        # credit order at 2.30: touch (mid==limit, natural below) does not fill
        assert not limit_fills(is_credit=True, limit=D("2.30"), natural=D("2.20"),
                               mid=D("2.30"), tick=TICK, through_ticks=1)
        # one tick through (mid 2.35) fills
        assert limit_fills(is_credit=True, limit=D("2.30"), natural=D("2.20"),
                           mid=D("2.35"), tick=TICK, through_ticks=1)
        # natural satisfying the limit fills regardless of mid
        assert limit_fills(is_credit=True, limit=D("2.30"), natural=D("2.30"),
                           mid=D("2.30"), tick=TICK, through_ticks=1)

    def test_sim02_debit_order_symmetric(self):
        # a buy order at 0.05: fills if buyable for <= 0.05
        assert limit_fills(is_credit=False, limit=D("0.05"), natural=D("0.05"),
                           mid=D("0.10"), tick=TICK, through_ticks=1)
        assert not limit_fills(is_credit=False, limit=D("0.05"), natural=D("0.10"),
                               mid=D("0.05"), tick=TICK, through_ticks=1)  # touch, not through

    def test_sim03_stop_fills_at_trigger_plus_slippage(self):
        assert stop_triggered(D("3.85"), D("3.80")) and not stop_triggered(D("3.75"), D("3.80"))
        # 3 ticks of slippage on a 0.05 tick = 0.15 worse than the 3.80 trigger
        assert stop_fill_price(D("3.80"), tick=TICK, slippage_ticks=3) == D("3.95")


class TestSimLedgerAndMargin:
    def test_sim04_cash_starts_and_posts_fills(self):
        led = SimLedger(cash=D("100000"))
        led.post_fill(D("230"), fee=D("2"))  # +2.30 x100 credit, $2 fees
        assert led.cash == D("100228")

    def test_sim04_margin_reduces_buying_power(self):
        led = SimLedger(cash=D("100000"))
        margin = spread_margin(width=D("50"), net_credit=D("4.00"))  # (50-4)*100 = 4600
        assert margin == D("4600")
        led.hold_margin(margin)
        assert led.buying_power == D("95400")
        led.release_margin(margin)
        assert led.buying_power == D("100000")


class TestSimulatedBroker:
    def test_credit_order_fills_through_and_posts_cash(self):
        b = SimulatedBroker(SimLedger(cash=D("100000")), tick=TICK)
        oid = asyncio.run(b.submit(condor_intent("2.30")))
        assert b.try_fill_limit(oid, natural=D("2.20"), mid=D("2.35"), is_credit=True)
        assert b.ledger.cash == D("100230")  # +2.30 x100
        assert asyncio.run(b.working_orders()) == []

    def test_stop_fills_with_slippage_and_stamps_paper(self):
        b = SimulatedBroker(SimLedger(), tick=TICK, stop_slippage_ticks=3)
        oid = asyncio.run(b.submit(stop_intent("PUT", "3.80")))
        price = b.try_fill_stop(oid, mark=D("3.85"))
        assert price == D("3.95")  # trigger + 3 ticks
        assert b._orders[oid].mode == "PAPER"  # SIM-05 stamp

    def test_stop_not_triggered_below_trigger(self):
        b = SimulatedBroker(SimLedger(), tick=TICK)
        oid = asyncio.run(b.submit(stop_intent("PUT", "3.80")))
        assert b.try_fill_stop(oid, mark=D("3.70")) is None
        assert len(asyncio.run(b.working_orders())) == 1
