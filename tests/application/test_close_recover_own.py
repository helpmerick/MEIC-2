"""Slice 4 unit tests: OwnershipLedger, CloseEntry, RecoverLong."""
import asyncio
from decimal import Decimal as D

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.recover_long import Quote, RecoverLong
from meic.domain.events import EntryClosed, LongSold, SideClosed
from meic.domain.ownership import Ownership, OwnershipLedger
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


class TestOwnershipLedger:
    def test_own01_ledger_only_from_own_fills(self):
        led = OwnershipLedger()
        led.apply_fill("SPXW_5990P", -2)  # bot sold 2 short puts
        assert led.owned("SPXW_5990P") == -2

    def test_own03_foreign_symbol_classified_and_capped_to_zero(self):
        led = OwnershipLedger()  # no bot fills on 6050 call
        assert led.classify("SPXW_6050C", broker_net=-1) is Ownership.FOREIGN
        assert led.cap_exit_qty("SPXW_6050C", 5) == 0  # OWN-04: submit nothing

    def test_own05_shared_symbol_when_operator_adds(self):
        led = OwnershipLedger()
        led.apply_fill("SPXW_5990P", -2)
        assert led.foreign_delta("SPXW_5990P", broker_net=-3) == -1
        assert led.classify("SPXW_5990P", broker_net=-3) is Ownership.SHARED
        assert led.cap_exit_qty("SPXW_5990P", 99) == 2  # capped to the bot's 2

    def test_own06_shortfall_when_broker_below_ledger(self):
        led = OwnershipLedger()
        led.apply_fill("SPXW_5990P", -2)
        assert led.classify("SPXW_5990P", broker_net=-1) is Ownership.SHORTFALL
        led.write_down_to("SPXW_5990P", -1)
        assert led.owned("SPXW_5990P") == -1


class TestCloseEntry:
    def _legs(self):
        return [LiveLeg("SPXW_5990P", "PUT", "short", -1), LiveLeg("SPXW_5940P", "PUT", "long", 1),
                LiveLeg("SPXW_6060C", "CALL", "short", -1), LiveLeg("SPXW_6110C", "CALL", "long", 1)]

    def test_cls02_rejects_unknown_initiator(self):
        import pytest
        with pytest.raises(ValueError):
            asyncio.run(CloseEntry(FakeBroker(), []).close(
                "e1", "kill_switch", resting_stop_ids=[], live_legs=[], close_price=D("0.05")))

    def test_records_initiator_and_closes_all_legs(self):
        broker, events = FakeBroker(), []
        asyncio.run(CloseEntry(broker, events).close(
            "e1", "manual", resting_stop_ids=["S1", "S2"], live_legs=self._legs(), close_price=D("0.05")))
        closed = [e for e in events if isinstance(e, EntryClosed)]
        assert len(closed) == 1 and closed[0].initiator == "manual"
        assert sum(isinstance(e, SideClosed) for e in events) == 4


class TestRecoverLong:
    def test_lex_ladder_then_fallback(self):
        broker, events = FakeBroker(), []  # never fills
        r = asyncio.run(RecoverLong(broker, events, SPX, lex_reprice_attempts=4).recover(
            entry_id="e1", side="PUT", long_symbol="SPXW_5940P",
            quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0")))
        assert r.outcome == "FALLBACK_WORKING"
        assert r.prices_tried == (D("2.15"), D("2.10"), D("2.05"), D("2.00"))
        # a marketable-limit fallback at the bid exists
        assert any(o.intent.get("type") == "marketable_limit" and o.intent["price"] == D("2.00")
                   for o in broker._orders.values())

    def test_lex_sells_and_records_recovery_on_fill(self):
        broker, events = FakeBroker(), []
        broker.script_submit(Scripted("fill", payload={"price": "2.15"}))
        r = asyncio.run(RecoverLong(broker, events, SPX).recover(
            entry_id="e1", side="PUT", long_symbol="SPXW_5940P",
            quote=Quote(bid=D("2.00"), ask=D("2.30")), intrinsic=D("0")))
        assert r.outcome == "SOLD"
        assert any(isinstance(e, LongSold) and e.recovery == D("2.15") for e in events)

    def test_lex02_unusable_quote_goes_straight_to_fallback(self):
        broker, events = FakeBroker(), []
        r = asyncio.run(RecoverLong(broker, events, SPX).recover(
            entry_id="e1", side="PUT", long_symbol="SPXW_5940P",
            quote=Quote(bid=D("2.40"), ask=D("2.30")), intrinsic=D("0")))  # crossed
        assert r.outcome == "FALLBACK_WORKING" and r.prices_tried == ()
