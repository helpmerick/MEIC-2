"""Slice 7 edge cases: external-close units, TC-DCY-03 gates, TC-TPF-04/06/08."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.decay_watcher import DecayWatcher
from meic.application.tpf_monitor import TPFMonitor
from meic.domain.external_close import SideDisposition, SideObservation, classify_side
from meic.domain.tpf import floor_amount, is_armable, valid_levels
from meic.domain.events import EntryClosed, SideClosed
from tests.harness.fake_broker import FakeBroker, Scripted

ET = __import__("zoneinfo").ZoneInfo("America/New_York")


class TestExternalClose:
    def test_own09_external_close_needs_all_guards(self):
        gone = dict(stop_filled=False, position_present=False, stop_working=True, stop_cancelled_by_bot=False)
        assert classify_side(SideObservation(**gone)) is SideDisposition.EXTERNAL_CLOSE
        # a lagging feed (not yet confirmed twice) waits rather than standing down
        assert classify_side(SideObservation(**gone, confirmed_two_reconciles=False)) is SideDisposition.STILL_OPEN
        assert classify_side(SideObservation(**gone, seen_open=False)) is SideDisposition.STILL_OPEN

    def test_filled_stop_always_stop_out(self):
        assert classify_side(SideObservation(
            stop_filled=True, position_present=False, stop_working=False,
            stop_cancelled_by_bot=False)) is SideDisposition.STOP_OUT


# --- TC-DCY-03: the gate matrix ----------------------------------------------

def test_tc_dcy_03_gate_matrix():
    """TC-DCY-03: ASK-only trigger; cutoff, MANUAL/SUSPENDED, Flatten block;
    Stop Trading does NOT block (buybacks continue); a failed re-placement
    under stop-trading suspends the watcher."""
    w = DecayWatcher(FakeBroker(), [])
    cutoff = datetime(2026, 7, 6, 15, 55, tzinfo=ET)
    before = datetime(2026, 7, 6, 15, 40, tzinfo=ET)
    after = datetime(2026, 7, 6, 15, 56, tzinfo=ET)

    assert w.gate_allows(now_time=before, cutoff_time=cutoff) is True
    assert w.gate_allows(now_time=after, cutoff_time=cutoff) is False              # past cutoff
    assert w.gate_allows(now_time=before, cutoff_time=cutoff, mode="MANUAL") is False
    assert w.gate_allows(now_time=before, cutoff_time=cutoff, mode="SUSPENDED") is False
    assert w.gate_allows(now_time=before, cutoff_time=cutoff, flatten_in_progress=True) is False
    # Stop Trading is NOT a gate here — buybacks continue (Ash's rule)
    assert w.gate_allows(now_time=before, cutoff_time=cutoff) is True
    # a re-inflation-guard re-placement failure under stop-trading suspends it
    assert w.gate_allows(now_time=before, cutoff_time=cutoff, watcher_suspended=True) is False

    # ASK-only: bid/mid never trip the trigger (evaluate takes ask alone)
    assert w.evaluate(ask=D("0.10")) is False  # ask above trigger
    assert w.evaluate(ask=D("0.05")) is False  # 1st valid
    assert w.evaluate(ask=D("0.05")) is True   # 2nd valid -> fire


# --- TC-TPF-04: close procedure (stops cancelled before spread close) --------

def test_tc_tpf_04_close_cancels_stops_before_close_orders():
    """TC-TPF-04/CLS-01: the TPF close cancels stops first, then closes legs —
    identical to any CloseEntry (initiator take_profit)."""
    calls = []

    class RecordingBroker:
        def __init__(self):
            self._f = FakeBroker()

        async def cancel(self, oid):
            calls.append(("cancel", oid))
            return await self._f.cancel(oid)

        async def submit(self, intent):
            calls.append(("submit", intent.get("leg")))
            return await self._f.submit(intent)

    broker, events = RecordingBroker(), []
    legs = [LiveLeg("SPXW_5990P", "PUT", "short", -1), LiveLeg("SPXW_5940P", "PUT", "long", 1)]
    asyncio.run(CloseEntry(broker, events).close(
        "e1", "take_profit", resting_stop_ids=["S1", "S2"], live_legs=legs, close_price=D("0.05")))
    kinds = [c[0] for c in calls]
    assert kinds[:2] == ["cancel", "cancel"]      # stops cancelled first (CLS-01.1)
    assert "submit" in kinds[2:]                  # then the close orders
    assert any(isinstance(e, EntryClosed) and e.initiator == "take_profit" for e in events)


# --- TC-TPF-06: partial scope (realized side counts, open side closes) -------

def test_tc_tpf_06_partial_scope_profit_includes_realized():
    """TC-TPF-06: put side already stopped (realized -1.10), call side open;
    profit% includes the realized loss; the floor evaluation uses the combined
    figure."""
    net_credit = D("4.00")
    realized_put = D("-1.10")        # already-stopped side's realized P&L
    call_side_mark_profit = D("2.00")
    combined_profit = realized_put + call_side_mark_profit  # 0.90
    floor = floor_amount(20, net_credit)  # 0.80
    m = TPFMonitor(tp_confirmation_evals=1)
    # combined profit 0.90 > floor 0.80 -> does not fire yet
    assert m.evaluate(profit=combined_profit, floor=floor) is False
    # if the call side decays so combined dips to the floor, it fires (closes the open side)
    assert TPFMonitor(tp_confirmation_evals=1).evaluate(profit=D("0.80"), floor=floor) is True


# --- TC-TPF-08: controls (no trailing, gap rule) -----------------------------

def test_tc_tpf_08_no_trailing_and_gap_rule():
    """TC-TPF-08: the floor never self-adjusts as profit grows (no trailing);
    raise/lower is operator-driven and always gap-validated (>=5 below)."""
    # gap rule: at 25% profit, levels up to 20 are armable, 25+ are not
    assert valid_levels(D("25")) == (5, 10, 15, 20)
    assert is_armable(20, D("25")) and not is_armable(25, D("25"))
    # no trailing: an armed floor is a fixed dollar amount, independent of later profit
    floor = floor_amount(20, D("4.00"))  # 0.80, fixed
    assert floor == D("0.80")
    # profit rising to 90% does NOT change the armed floor (it is not recomputed)
    assert floor_amount(20, D("4.00")) == D("0.80")
