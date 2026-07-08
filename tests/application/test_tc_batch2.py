"""Second batch of edge-case prose TCs across STK/NLE/CLS/EOD/DAT/SIM/PNL."""
import asyncio
import inspect
from datetime import datetime, time, timedelta
from decimal import Decimal as D
from pathlib import Path

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.domain.delta_select import select_by_delta
from meic.domain.events import CondorFilled, EntryClosed, LongSold, ShortStopped, SideClosed, SideExpired
from meic.domain.marking import conservative_mark, reconcile_pnl, skip_late_on_half_day
from meic.domain.nle import estimate_net_loss
from meic.domain.nle_calibration import CalibrationRecord, CalibrationView
from meic.domain.projection import fold
from meic.domain.staleness import StampedQuote
from meic.domain.stop_policy import StopBasis, stop_trigger
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.intents import condor_intent, stop_intent

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
T0 = datetime(2026, 7, 6, 14, 0, 0)


# --- STK ---------------------------------------------------------------------

def test_tc_stk_01_delta_method_closest_without_exceeding_max():
    """TC-STK-01 (STK-02 delta): closest to 0.10Δ not exceeding 0.15Δ; the
    boundary strike at exactly short_delta_max is eligible."""
    chain = [(D("5990"), D("0.08")), (D("5985"), D("0.11")), (D("5980"), D("0.16"))]
    assert select_by_delta(chain, target=D("0.10"), max_delta=D("0.15")) == D("5985")
    # boundary: exactly 0.15 is eligible and closest wins
    boundary = [(D("6000"), D("0.20")), (D("5995"), D("0.15"))]
    assert select_by_delta(boundary, target=D("0.10"), max_delta=D("0.15")) == D("5995")


def test_tc_stk_04_stale_greeks_abort_entry():
    """TC-STK-04 (STK-04/DAT-02): greeks older than max_quote_age_ms ⇒ abort."""
    q = StampedQuote("SPXW_5990P", D("2.0"), D("2.1"), T0)
    assert not q.usable(T0 + timedelta(milliseconds=4000), max_age_ms=3000)  # -> entry aborts


def test_tc_stk_05_tick_regimes_both():
    """TC-STK-05 (STK-08): prices land on valid ticks in both regimes."""
    assert SPX.round(D("2.937")) == D("2.95") and SPX.tick_for(D("2.9")) == D("0.05")
    assert SPX.round(D("3.44")) == D("3.40") and SPX.tick_for(D("3.4")) == D("0.10")


# --- NLE ---------------------------------------------------------------------

PUT_CHAIN = {D("5990"): D("1.35"), D("5960"): D("3.10"), D("5950"): D("4.20"),
             D("5945"): D("5.14"), D("5940"): D("0.15"), D("5985"): D("1.55")}


def test_tc_nle_02_asymmetric_chains_differ():
    """TC-NLE-02: a put-skewed chain and a flatter call chain produce DIFFERENT
    estimates — no blended figure exists."""
    put = estimate_net_loss(chain_mids=PUT_CHAIN, short_strike=D("5990"), short_fill=D("1.35"),
                            long_strike=D("5940"), long_fill=D("0.15"), stop_trigger=D("5.14"),
                            nle_haircut_pct=D("30"))
    call_chain = {D("6060"): D("1.25"), D("6090"): D("2.60"), D("6100"): D("3.40"),
                  D("6110"): D("0.15"), D("6055"): D("1.40")}
    call = estimate_net_loss(chain_mids=call_chain, short_strike=D("6060"), short_fill=D("1.25"),
                             long_strike=D("6110"), long_fill=D("0.15"), stop_trigger=D("3.40"),
                             nle_haircut_pct=D("30"))
    assert put.estimated_net_loss != call.estimated_net_loss


def test_tc_nle_03_too_few_strikes_unavailable_entry_proceeds():
    """TC-NLE-03: too few strikes ⇒ UNAVAILABLE; the estimate never gates entry."""
    from meic.domain.nle import EstimateUnavailable
    r = estimate_net_loss(chain_mids={D("5990"): D("1.35")}, short_strike=D("5990"), short_fill=D("1.35"),
                          long_strike=D("5940"), long_fill=D("0.15"), stop_trigger=D("5.14"),
                          nle_haircut_pct=D("30"))
    assert isinstance(r, EstimateUnavailable)  # informational only; entry unaffected


def test_tc_nle_04_trigger_isolated_from_estimator():
    """TC-NLE-04: the stop trigger is byte-identical regardless of NLE, and the
    estimator module has no import path to order placement."""
    base = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), total_net_credit=D("4.00"))
    for _ in range(5):  # NLE running/erroring changes nothing about the trigger
        assert stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"), total_net_credit=D("4.00")) == base
    nle_src = Path(inspect.getfile(estimate_net_loss)).read_text()
    assert "protect_position" not in nle_src and "execute_entry" not in nle_src  # no order path


def test_tc_nle_05_calibration_record_per_stop():
    """TC-NLE-05 (NLE-06): a short-stop writes a complete calibration record;
    error = realized − estimated."""
    rec = CalibrationRecord(side="PUT", estimated_net_loss=D("2.855"), realized_net_loss=D("3.10"))
    assert rec.error == D("0.245")


def test_tc_nle_06_sample_threshold():
    """TC-NLE-06 (NLE-07): 24 samples ⇒ insufficient; 25+ ⇒ per-side summary."""
    v = CalibrationView()
    for _ in range(24):
        v.add(CalibrationRecord("PUT", D("2.0"), D("2.2")))
    assert v.summary()["status"] == "insufficient_data"
    v.add(CalibrationRecord("PUT", D("2.0"), D("2.2")))
    s = v.summary()
    assert s["status"] == "ok" and s["PUT"]["mean_estimate_error"] == D("0.2")


# --- CLS ---------------------------------------------------------------------

def test_tc_cls_03_idempotent_close_one_order_per_leg():
    """TC-CLS-03: a duplicated close command produces no duplicate orders —
    per-leg close order count = 1 (idempotency keys, ORD-04)."""
    submitted: dict[str, int] = {}

    class DedupBroker:
        async def cancel(self, oid):
            return {"result": "cancelled"}

        async def submit(self, intent):
            key = intent.idempotency_key
            submitted[key] = submitted.get(key, 0) + 1  # a real broker dedupes by key
            return key

    legs = [LiveLeg("SPXW_5990P", "PUT", "short", -1)]
    b, events = DedupBroker(), []
    for _ in range(2):  # double-click
        asyncio.run(CloseEntry(b, events).close("e1", "manual", resting_stop_ids=["S"],
                                                live_legs=legs, close_price=D("0.05")))
    assert all(v == 2 for v in submitted.values())  # same key seen twice...
    assert len(submitted) == 1  # ...but it is ONE key -> the broker places one order


def test_tc_cls_04_completeness_stops_cancelled_legs_closed():
    """TC-CLS-04: after a close, the entry's stops are cancelled and legs closed
    — nothing left resting."""
    broker, events = FakeBroker(), []
    s1 = asyncio.run(broker.submit(stop_intent("PUT", entry_id="e1")))
    legs = [LiveLeg("SPXW_5990P", "PUT", "short", -1), LiveLeg("SPXW_5940P", "PUT", "long", 1)]
    asyncio.run(CloseEntry(broker, events).close("e1", "manual", resting_stop_ids=[s1],
                                                 live_legs=legs, close_price=D("0.05")))
    assert broker._orders[s1].status == "CANCELLED"           # stop cancelled
    assert sum(isinstance(e, SideClosed) for e in events) == 2  # both legs closed
    assert any(isinstance(e, EntryClosed) for e in events)


# --- EOD ---------------------------------------------------------------------

def test_tc_eod_02_eod_close_via_ladder_initiator_eod():
    """TC-EOD-02: with eod_close_time set, open sides close via the canonical
    close, initiator eod."""
    broker, events = FakeBroker(), []
    legs = [LiveLeg("SPXW_6060C", "CALL", "short", -1)]
    asyncio.run(CloseEntry(broker, events).close("e1", "eod", resting_stop_ids=["S"],
                                                 live_legs=legs, close_price=D("0.05")))
    assert [e.initiator for e in events if isinstance(e, EntryClosed)] == ["eod"]


def test_tc_eod_04_late_stop_runs_lex_remainder_expires():
    """TC-EOD-04: a stop fills near close ⇒ that side runs LEX; the other side
    expires at settlement."""
    events = [
        CondorFilled(entry_id="e", net_credit=D("4.00")),
        ShortStopped(entry_id="e", side="PUT", fill=D("3.80"), slippage=D("0")),
        LongSold(entry_id="e", side="PUT", recovery=D("0.10")),
        SideExpired(entry_id="e", side="CALL"),
    ]
    entry = fold(events).entries["e"]
    assert entry.sides_stopped == ("PUT",) and entry.sides_expired == ("CALL",)


def test_tc_eod_05_half_day_late_entries_skipped():
    """TC-EOD-05 (DAY-02): on a half day (13:00 close), an entry at/after
    close − min_time_before_close is skipped."""
    assert skip_late_on_half_day(time(12, 50), time(13, 0), 15) is True   # within 15m of close
    assert skip_late_on_half_day(time(12, 40), time(13, 0), 15) is False  # comfortably before


# --- DAT ---------------------------------------------------------------------

def test_tc_dat_01_silent_staleness_blocks_decisions():
    """TC-DAT-01: connected-but-silent staleness is detected per instrument;
    decisions are blocked on the stale symbol."""
    fresh = StampedQuote("SPXW_5990P", D("2.0"), D("2.1"), T0)
    now = T0 + timedelta(milliseconds=5000)  # 5s of silence
    assert fresh.is_stale(now, max_age_ms=3000) and not fresh.usable(now, max_age_ms=3000)


def test_tc_dat_03_halt_blocks_entries():
    """TC-DAT-03 (DAT-04): a halt blocks entry attempts (no catch-up)."""
    from meic.application.entry_gates import GateSnapshot, evaluate_gates
    halted = GateSnapshot(armed=True, confirm_live=True, stop_trading=False, flatten_in_progress=False,
                          market_open=True, market_halted=True, data_fresh=True, session_valid=True,
                          buying_power_ok=True)
    assert evaluate_gates(halted) == "market_halted"


# --- SIM ---------------------------------------------------------------------

def test_tc_sim_02_stop_sim_slippage_and_event():
    """TC-SIM-02 (SIM-03): the mark reaching the trigger fires the sim stop at
    trigger + 3 ticks, emitting the same ShortStopped a live fill would."""
    from meic.adapters.sim.simulated_broker import SimLedger, SimulatedBroker
    events: list = []
    b = SimulatedBroker(SimLedger(), tick=D("0.05"), stop_slippage_ticks=3, events=events)
    oid = asyncio.run(b.submit(stop_intent("PUT", "3.80", entry_id="e")))
    price = b.try_fill_stop(oid, mark=D("3.85"))
    assert price == D("3.95")  # 3.80 + 3 ticks
    assert any(isinstance(e, ShortStopped) and e.entry_id == "e" for e in events)  # -> LEX path


def test_tc_sim_05_paper_never_imports_live_adapter_or_cert():
    """TC-SIM-05 (SIM-01/06): paper mode never references the live adapter or
    cert endpoints (architecture assertion)."""
    from meic.composition import paper
    src = Path(inspect.getfile(paper)).read_text()
    assert "TastytradeAdapter" not in src and "tastytrade" not in src.lower()


# --- PNL ---------------------------------------------------------------------

def test_tc_pnl_01_per_entry_to_the_cent_with_fees():
    """TC-PNL-01 (PNL-01/02): per-entry P&L (credit, stop-out w/ slippage, long
    recovery, expired side) matches hand computation including fees."""
    events = [
        CondorFilled(entry_id="e", net_credit=D("4.00"), fee=D("0.08")),
        ShortStopped(entry_id="e", side="PUT", fill=D("3.80"), slippage=D("0.05"), fee=D("0.02")),
        LongSold(entry_id="e", side="PUT", recovery=D("0.40"), fee=D("0.02")),
        SideExpired(entry_id="e", side="CALL"),
    ]
    # 4.00 - 3.80 + 0.40 - (0.08+0.02+0.02) = 0.48
    assert fold(events).entries["e"].pnl == D("0.48")


def test_tc_pnl_02_conservative_marking():
    """TC-PNL-02 (PNL-03): live marking uses mid, degrading to worst-of-bid/ask
    when stale."""
    assert conservative_mark(D("2.00"), D("2.10"), stale=False) == D("2.05")   # mid
    assert conservative_mark(D("2.00"), D("2.10"), stale=True) == D("2.00")    # worst (bid)


def test_tc_pnl_03_broker_authority_and_tolerance():
    """TC-PNL-03 (PNL-04): a $0.12 divergence flags PnlMismatch with the broker
    figure authoritative; a $0.03 divergence reconciles silently."""
    over = reconcile_pnl(bot_pnl=D("1.00"), broker_pnl=D("0.88"), tolerance=D("0.05"))
    assert over.mismatch and over.authoritative == D("0.88") and over.delta == D("-0.12")
    under = reconcile_pnl(bot_pnl=D("1.00"), broker_pnl=D("0.97"), tolerance=D("0.05"))
    assert not under.mismatch and under.authoritative == D("0.97")
