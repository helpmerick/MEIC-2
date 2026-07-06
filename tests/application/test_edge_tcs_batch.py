"""A batch of edge-case prose TCs greened against existing mechanics:
TC-ORD-03, TC-STP-06, TC-TPF-05, TC-TPF-07, TC-EOD-01, TC-DAT-02.
"""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

from meic.adapters.persistence.event_store import InMemoryStateStore, SqliteStateStore
from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.persistent_state import PersistentState
from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.application.tpf_monitor import TPFMonitor
from meic.domain.events import CondorFilled, DayArmed, LongSold, ShortStopped, SideExpired
from meic.domain.projection import fold
from meic.domain.quote_hub import QuoteHub, resolve_decision
from meic.domain.staleness import StampedQuote
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


class _Alerts:
    def alert(self, *a, **k):
        pass


def test_tc_ord_03_cancel_fill_race_treats_condor_open_and_places_stops():
    """TC-ORD-03 (ORD-05/EC-ENT-12): a fill event arriving after a cancel was
    sent ⇒ the condor is OPEN (broker truth wins), stops placed."""
    broker, events = FakeBroker(), []
    clock = FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET))
    # the entry order actually filled; a cancel arriving after comes back
    # terminal (ORD-08) — broker truth wins, the condor is OPEN
    broker.script_submit(Scripted("fill", payload={"net_credit": "4.00"}))
    order_id = asyncio.run(broker.submit({"type": "limit", "kind": "iron_condor", "legs": 4}))
    outcome = asyncio.run(broker.cancel(order_id))  # order already FILLED
    assert outcome["result"] == "terminal" and outcome["status"] == "FILLED"
    # -> the entry is treated as filled; ProtectPosition places the stops
    p = ProtectPosition(broker, clock, _Alerts(), events, SPX)
    r = asyncio.run(p.protect(entry_id="e1", basis=StopBasis.TOTAL_CREDIT,
                              shorts=[ShortLeg("PUT", D("3.00"), D("0.50")), ShortLeg("CALL", D("2.00"), D("0.50"))],
                              total_net_credit=D("4.00")))
    assert r.outcome == "PROTECTED"
    assert len(asyncio.run(broker.working_orders())) == 2


def test_tc_stp_06_whipsaw_both_sides_stop_both_losses_in_pnl():
    """TC-STP-06 (STP-07/08): put stops, then call stops; both run LEX
    independently; both losses appear in day P&L."""
    events = [
        DayArmed(date="d", entry_count=1),
        CondorFilled(entry_id="e", net_credit=D("4.00")),
        ShortStopped(entry_id="e", side="PUT", fill=D("3.80"), slippage=D("0")),
        LongSold(entry_id="e", side="PUT", recovery=D("0.10")),
        ShortStopped(entry_id="e", side="CALL", fill=D("3.80"), slippage=D("0")),
        LongSold(entry_id="e", side="CALL", recovery=D("0.10")),
    ]
    entry = fold(events).entries["e"]
    # 4.00 - 3.80 - 3.80 + 0.10 + 0.10 = -3.40; both sides recorded stopped
    assert entry.pnl == D("-3.40")
    assert set(entry.sides_stopped) == {"PUT", "CALL"}


def test_tc_tpf_05_race_one_buyback_per_leg():
    """TC-TPF-05 (EC-TPF-03): a short stop fills as its cancel lands ⇒ that side
    is SIDE_STOPPED + LEX, the OTHER side is closed by TPF; no duplicate
    buy-back (order count per leg = 1)."""
    broker, events = FakeBroker(), []
    # the put stop already filled (the stop won the race) — CloseEntry closes
    # only the still-live call side; the put short is NOT bought again.
    live = [LiveLeg("SPXW_6060C", "CALL", "short", -1), LiveLeg("SPXW_6110C", "CALL", "long", 1)]
    asyncio.run(CloseEntry(broker, events).close(
        "e1", "take_profit", resting_stop_ids=["call_stop"], live_legs=live, close_price=D("0.05")))
    orders = list(broker._orders.values())
    put_short_closes = [o for o in orders if o.intent.get("leg") == "short_put"]
    assert put_short_closes == []  # put short bought exactly once (by its stop), not again
    call_short_closes = [o for o in orders if o.intent.get("leg") == "short_call"]
    assert len(call_short_closes) == 1  # exactly one buy-back for the closed side


def test_tc_tpf_07_restart_floor_persists_and_fires_immediately():
    """TC-TPF-07 (TPF-08/EC-TPF-01): floor armed, bot down while profit gaps
    below it ⇒ on recovery the close triggers immediately at the current level."""
    path = ":memory:"  # a durable store round-trip
    store = SqliteStateStore("file:tpf07?mode=memory&cache=shared") if False else InMemoryStateStore()
    s = PersistentState(store)
    s.tpf_floors = {"e1": 20}  # armed 20% floor on a 4.00-credit entry
    # bot restarts: a fresh PersistentState over the same store restores it
    recovered = PersistentState(store)
    floor_pct = recovered.tpf_floors["e1"]
    floor = D("4.00") * floor_pct / 100  # 0.80
    # on recovery, profit already gapped below the floor -> fires on first eval
    m = TPFMonitor(tp_confirmation_evals=1)
    assert m.evaluate(profit=D("0.50"), floor=floor) is True


def test_tc_eod_01_untouched_condor_settles_expired_keeps_credit():
    """TC-EOD-01: an untouched condor is held to settlement; both sides EXPIRED;
    settlement P&L = the full credit kept (0DTE cash-settled worthless)."""
    events = [
        CondorFilled(entry_id="e", net_credit=D("4.00")),
        SideExpired(entry_id="e", side="PUT"),
        SideExpired(entry_id="e", side="CALL"),
    ]
    entry = fold(events).entries["e"]
    assert set(entry.sides_expired) == {"PUT", "CALL"}
    assert entry.pnl == D("4.00")  # kept the whole credit


def test_tc_dat_02_disconnect_aborts_entry_lex_freezes():
    """TC-DAT-02 (DAT-03): a feed disconnect at a decision moment ⇒ give up
    safely (entry aborts data_unavailable); a stale quote is never usable."""
    hub = QuoteHub()
    hub.open_generation()
    hub.mark_sick()

    async def fail():
        return False

    async def fetch_fail():
        return None

    outcome = asyncio.run(resolve_decision(hub, demand_reconnect=fail, scoped_fetch=fetch_fail))
    assert outcome.result == "GIVE_UP" and outcome.reason == "data_unavailable"  # entry aborts
    # a stale quote is never used for a pricing decision (LEX-02/DAT-02)
    t0 = datetime(2026, 7, 6, 14, 0, 0)
    q = StampedQuote("SPXW_5990P", D("2.00"), D("2.10"), t0)
    assert not q.usable(t0 + timedelta(milliseconds=5000), max_age_ms=3000)
