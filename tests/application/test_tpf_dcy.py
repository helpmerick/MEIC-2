"""TPFMonitor + DecayWatcher — unit tests and prose TCs (TPF-03/09, DCY-01..04).

test_tc_* functions are the hand-written prose-TC implementations (the
generator skips prose TCs that have a hand-written test).
"""
import asyncio
from decimal import Decimal as D

from meic.application.decay_watcher import DecayWatcher
from meic.application.tpf_monitor import TPFMonitor
from meic.domain.events import EntryClosed, LongSold, ShortStopped
from tests.harness.fake_broker import FakeBroker, Scripted


# --- TPFMonitor --------------------------------------------------------------

class TestTPFMonitor:
    def test_confirmation_evals_required(self):
        m = TPFMonitor(tp_confirmation_evals=2)
        assert m.evaluate(profit=D("0.80"), floor=D("0.80")) is False   # 1st
        assert m.evaluate(profit=D("0.80"), floor=D("0.80")) is True    # 2nd -> fire

    def test_single_bad_print_does_not_fire(self):
        m = TPFMonitor(tp_confirmation_evals=2)
        assert m.evaluate(profit=D("0.80"), floor=D("0.80")) is False
        assert m.evaluate(profit=D("2.00"), floor=D("0.80")) is False   # recovers -> reset
        assert m.evaluate(profit=D("0.80"), floor=D("0.80")) is False   # counter restarted

    def test_stale_pauses_and_resets(self):
        m = TPFMonitor(tp_confirmation_evals=2)
        assert m.evaluate(profit=D("0.80"), floor=D("0.80")) is False
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), stale=True) is False  # EC-TPF-02
        assert m.evaluate(profit=D("0.80"), floor=D("0.80")) is False  # streak reset


def test_tc_tpf_03_trigger_mechanics():
    """TC-TPF-03: floor 20% on $4.00 (=$0.80) fires after 2 valid evals; a
    single bad print doesn't; stale pauses+resets."""
    m = TPFMonitor(tp_confirmation_evals=2)
    floor = D("4.00") * 20 / 100  # 0.80
    assert m.evaluate(profit=D("0.75"), floor=floor) is False
    assert m.evaluate(profit=D("0.75"), floor=floor) is True
    # a lone print below the floor never fires
    m2 = TPFMonitor(tp_confirmation_evals=2)
    assert m2.evaluate(profit=D("0.70"), floor=floor) is False
    assert m2.evaluate(profit=D("5.00"), floor=floor) is False


# --- DecayWatcher ------------------------------------------------------------

class TestDecayWatcher:
    def test_dcy01_ask_only_two_evals(self):
        w = DecayWatcher(FakeBroker(), [], decay_confirmation_evals=2)
        assert w.evaluate(ask=D("0.05")) is False
        assert w.evaluate(ask=D("0.05")) is True

    def test_stale_or_high_ask_resets(self):
        w = DecayWatcher(FakeBroker(), [], decay_confirmation_evals=2)
        assert w.evaluate(ask=D("0.05")) is False
        assert w.evaluate(ask=D("0.05"), stale=True) is False  # reset
        assert w.evaluate(ask=D("0.05")) is False              # restarts


def test_tc_dcy_01_happy_path():
    """TC-DCY-01 (DCY-01/02/03): ask<=0.05 x2 -> cancel stop -> buy at trigger
    -> fill -> SIDE_CLOSED_DECAY, P&L realized, long RETAINED (no LEX sale)."""
    broker, events = FakeBroker(), []
    resting = asyncio.run(broker.submit({"type": "stop_market", "leg": "short_put"}))
    w = DecayWatcher(broker, events)
    assert w.evaluate(ask=D("0.05")) is False
    assert w.evaluate(ask=D("0.05")) is True
    result = asyncio.run(w.buyback(entry_id="e1", side="PUT", resting_stop_id=resting))
    assert result != "STOP_FILLED_RUN_LEX"
    asyncio.run(w.complete(entry_id="e1", side="PUT"))
    assert any(isinstance(e, ShortStopped) and e.initiator == "decay" for e in events)
    assert any(isinstance(e, EntryClosed) and e.initiator == "decay" for e in events)
    assert not any(isinstance(e, LongSold) for e in events)  # long left to expire (DCY-03)


def test_tc_dcy_02_reinflation_guard():
    """TC-DCY-02 (DCY-02.3): ask jumps to 0.30 before fill -> cancel buyback,
    re-place the resting stop; a stop that actually FILLED runs LEX."""
    broker, events = FakeBroker(), []
    resting = asyncio.run(broker.submit({"type": "stop_market", "leg": "short_put"}))
    w = DecayWatcher(broker, events)
    buyback_id = asyncio.run(w.buyback(entry_id="e1", side="PUT", resting_stop_id=resting))
    outcome = asyncio.run(w.reinflation_guard(
        entry_id="e1", side="PUT", buyback_id=buyback_id, resting_stop_id=resting,
        current_ask=D("0.30"), unfilled=True))
    assert outcome.startswith("REPROTECTED:")  # protection restored

    # if the resting stop had actually filled, the buyback aborts to LEX
    b2, e2 = FakeBroker(), []
    rid = asyncio.run(b2.submit({"type": "stop_market", "leg": "short_put"}))
    b2._orders[rid].status = "FILLED"
    w2 = DecayWatcher(b2, e2)
    assert asyncio.run(w2.buyback(entry_id="e2", side="PUT", resting_stop_id=rid)) == "STOP_FILLED_RUN_LEX"


def test_tc_dcy_04_routes_through_canonical_close_initiator_decay():
    """TC-DCY-04 (DCY-02/CLS-02): the buyback close is recorded with initiator
    `decay`; no separate close path exists (the EntryClosed carries `decay`)."""
    broker, events = FakeBroker(), []
    w = DecayWatcher(broker, events)
    asyncio.run(w.complete(entry_id="e9", side="CALL"))
    closes = [e for e in events if isinstance(e, EntryClosed)]
    assert len(closes) == 1 and closes[0].initiator == "decay"
