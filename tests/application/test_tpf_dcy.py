"""TPFMonitor + DecayWatcher — unit tests and prose TCs (TPF-03/09, DCY-01..04).

test_tc_* functions are the hand-written prose-TC implementations (the
generator skips prose TCs that have a hand-written test).
"""
import asyncio
from decimal import Decimal as D

import pytest

from meic.application.decay_watcher import DecayWatcher
from meic.application.tpf_monitor import TPFMonitor
from meic.domain.events import EntryClosed, LongSold, ShortStopped
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.intents import condor_intent, stop_intent


# --- TPFMonitor --------------------------------------------------------------

# TPF-03b(ii) — THE MIGRATION IS VERIFIED AGAINST A NON-DEFAULT DURATION.
# 2 evals x the 250 ms cadence = 500 ms, which is EXACTLY tp_confirmation_ms's
# default, so a count-shaped regression would still pass every test written at
# the default. These use 1500 ms deliberately: at the default, "fires on the
# second evaluation" and "fires after 500 ms" are indistinguishable.
CONFIRM_MS = 1500


class TestTPFMonitor:
    def test_confirmation_is_a_duration_not_a_count(self):
        """The breach must hold for the full DURATION. Evaluation COUNT is
        irrelevant: a hundred evaluations inside the window still do not fire."""
        m = TPFMonitor(tp_confirmation_ms=CONFIRM_MS)
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=0) is False
        for t in range(50, CONFIRM_MS, 50):          # 29 more evaluations, all inside
            assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=t) is False, (
                f"fired at {t}ms, before the {CONFIRM_MS}ms confirmation elapsed -- "
                "this is the count-shaped regression TPF-03b retired")
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=CONFIRM_MS) is True

    def test_a_slow_cadence_fires_on_the_same_wall_clock(self):
        """The point of a duration: the SAME two evaluations, spaced far apart,
        fire because time passed -- not because there were two of them."""
        m = TPFMonitor(tp_confirmation_ms=CONFIRM_MS)
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=0) is False
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=CONFIRM_MS) is True

    def test_zero_confirmation_fires_on_the_first_valid_breach(self):
        """TPF-03b, explicit: `tp_confirmation_ms = 0` fires immediately."""
        m = TPFMonitor(tp_confirmation_ms=0)
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=12345) is True

    def test_recovery_CLEARS_the_elapsed_time_never_pauses_it(self):
        """TPF-03b: a recovery CLEARS. An accumulator would let a flickering
        mark bank progress across recoveries and fire on a breach that was
        never continuous -- so after recovering, the full duration restarts."""
        m = TPFMonitor(tp_confirmation_ms=CONFIRM_MS)
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=0) is False
        assert m.evaluate(profit=D("2.00"), floor=D("0.80"), now_ms=1400) is False  # recovers
        # 1400ms of breach was banked before the recovery. If it were PAUSED
        # rather than CLEARED, 100ms more would fire. It must not.
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=1500) is False
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=1500 + CONFIRM_MS) is True

    def test_stale_CLEARS_the_elapsed_time_never_pauses_it(self):
        """EC-TPF-02. An invalid evaluation is not evidence the breach
        continued, so it cannot count toward a CONTINUOUS breach."""
        m = TPFMonitor(tp_confirmation_ms=CONFIRM_MS)
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=0) is False
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), stale=True, now_ms=1400) is False
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=1500) is False
        assert m.evaluate(profit=D("0.80"), floor=D("0.80"), now_ms=1500 + CONFIRM_MS) is True

    def test_tp_confirmation_evals_is_tombstoned_at_the_constructor(self):
        """The retired parameter must not be quietly accepted and ignored --
        that would let an operator believe a tuned count was still in force."""
        with pytest.raises(TypeError):
            TPFMonitor(tp_confirmation_evals=2)


def test_tc_tpf_03_trigger_mechanics():
    """TC-TPF-03: floor 20% on $4.00 (=$0.80) fires once the breach has held
    for the confirmation DURATION; a single bad print doesn't; stale clears."""
    m = TPFMonitor(tp_confirmation_ms=CONFIRM_MS)
    floor = D("4.00") * 20 / 100  # 0.80
    assert m.evaluate(profit=D("0.75"), floor=floor, now_ms=0) is False
    assert m.evaluate(profit=D("0.75"), floor=floor, now_ms=CONFIRM_MS) is True
    # a lone print below the floor never fires
    m2 = TPFMonitor(tp_confirmation_ms=CONFIRM_MS)
    assert m2.evaluate(profit=D("0.70"), floor=floor, now_ms=0) is False
    assert m2.evaluate(profit=D("5.00"), floor=floor, now_ms=CONFIRM_MS) is False


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
    resting = asyncio.run(broker.submit(stop_intent("PUT")))
    w = DecayWatcher(broker, events)
    assert w.evaluate(ask=D("0.05")) is False
    assert w.evaluate(ask=D("0.05")) is True
    result = asyncio.run(w.buyback(entry_id="e1", side="PUT", resting_stop_id=resting, symbol="SPXW  260707P05990000"))
    assert result != "STOP_FILLED_RUN_LEX"
    asyncio.run(w.complete(entry_id="e1", side="PUT"))
    assert any(isinstance(e, ShortStopped) and e.initiator == "decay" for e in events)
    assert any(isinstance(e, EntryClosed) and e.initiator == "decay" for e in events)
    assert not any(isinstance(e, LongSold) for e in events)  # long left to expire (DCY-03)
    # PNL-01: a decay buyback is a CLOSE (commission-free), but clearing/ORF/
    # exchange still apply -- never the bare 0 default. Per-share: real
    # $0.72 / 100.
    decayed = next(e for e in events if isinstance(e, ShortStopped) and e.initiator == "decay")
    assert decayed.fee == D("0.0072")


def test_tc_dcy_02_reinflation_guard():
    """TC-DCY-02 (DCY-02.3): ask jumps to 0.30 before fill -> cancel buyback,
    re-place the resting stop; a stop that actually FILLED runs LEX."""
    broker, events = FakeBroker(), []
    resting = asyncio.run(broker.submit(stop_intent("PUT")))
    w = DecayWatcher(broker, events)
    buyback_id = asyncio.run(w.buyback(entry_id="e1", side="PUT", resting_stop_id=resting, symbol="SPXW  260707P05990000"))
    outcome = asyncio.run(w.reinflation_guard(
        entry_id="e1", side="PUT", buyback_id=buyback_id, resting_stop_id=resting,
        current_ask=D("0.30"), unfilled=True,
        symbol="SPXW  260707P05990000", trigger=D("3.80")))
    assert outcome.startswith("REPROTECTED:")  # protection restored

    # if the resting stop had actually filled, the buyback aborts to LEX
    b2, e2 = FakeBroker(), []
    rid = asyncio.run(b2.submit(stop_intent("PUT")))
    b2._orders[rid].status = "FILLED"
    w2 = DecayWatcher(b2, e2)
    assert asyncio.run(w2.buyback(entry_id="e2", side="PUT", resting_stop_id=rid, symbol="SPXW  260707P05990000")) == "STOP_FILLED_RUN_LEX"


def test_tc_dcy_04_routes_through_canonical_close_initiator_decay():
    """TC-DCY-04 (DCY-02/CLS-02): the buyback close is recorded with initiator
    `decay`; no separate close path exists (the EntryClosed carries `decay`)."""
    broker, events = FakeBroker(), []
    w = DecayWatcher(broker, events)
    asyncio.run(w.complete(entry_id="e9", side="CALL"))
    closes = [e for e in events if isinstance(e, EntryClosed)]
    assert len(closes) == 1 and closes[0].initiator == "decay"
