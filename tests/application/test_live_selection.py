"""LiveCondorSelector + LiveMarketGates + chain snapshot assembly.

The point of these tests: bad data must NEVER produce a Condor. Every degraded
input yields a named skip reason and no order.
"""
import asyncio
from datetime import date, datetime, time
from decimal import Decimal as D

import pytest

from meic.adapters.dxlink.chain_snapshot import build_sides
from meic.application.market_calendar import is_market_open
from meic.composition.live_gates import LiveMarketGates
from meic.composition.live_selection import LiveCondorSelector, SelectionConfig
from meic.domain.chain import ChainSide, Mark
from tests.harness.fake_clock import ET

SPOT = D("6000")
CFG = SelectionConfig(target_premium=D("3.00"), wing_width=D("50"),
                      min_short_premium=D("1.00"), min_total_credit=D("2.00"),
                      completeness_pct=D("90"))


class Snap:
    """Stand-in for ChainSnapshot."""
    def __init__(self, put_side, call_side, put_band, call_band, stale=False):
        self.put_side, self.call_side = put_side, call_side
        self.put_band, self.call_band = put_band, call_band
        self.stale = stale


def _side(strikes, mids, *, descending):
    ordered = tuple(sorted(strikes, reverse=descending))
    marks = {D(str(k)): Mark(bid=D(str(m)) - D("0.05"), ask=D(str(m)) + D("0.05"))
             for k, m in mids.items()}
    return ChainSide(ordered, marks)


def _healthy_snapshot(**over):
    """Puts below spot, calls above. Mids decay away from the money like a real
    0DTE chain: the ~3.00 short sits 2 strikes OTM and its 50-wide wing is cheap,
    so net credit comfortably clears the 2.00 floor (STK-06)."""
    put_strikes = [D(str(6000 - 5 * i)) for i in range(0, 25)]
    call_strikes = [D(str(6000 + 5 * i)) for i in range(0, 25)]
    # i=0 -> 3.60 (ATM), i=2 -> 3.00 (the short), i=12 (50 pts away) -> 0.15 wing
    curve = lambda i: max(0.15, round(3.60 - 0.30 * i, 2))
    put_mids = {int(k): curve(i) for i, k in enumerate(put_strikes)}
    call_mids = {int(k): curve(i) for i, k in enumerate(call_strikes)}
    puts = _side(put_strikes, put_mids, descending=True)
    calls = _side(call_strikes, call_mids, descending=False)
    band_p = tuple(k for k in puts.strikes_toward_otm if SPOT - k <= 120)
    band_c = tuple(k for k in calls.strikes_toward_otm if k - SPOT <= 120)
    return Snap(puts, calls, band_p, band_c, **over)


def _select(snap, occupancy=None, cfg=CFG):
    sel = LiveCondorSelector(snapshot_provider=lambda: _async(snap), config=cfg,
                             occupancy_provider=(lambda: occupancy or {}))
    return asyncio.run(sel(datetime(2026, 7, 8, 10, 0, tzinfo=ET), 1))


async def _async(v):
    return v


# --- happy path ---------------------------------------------------------------

def test_healthy_chain_yields_a_condor_that_clears_the_credit_gates():
    condor, reason = _select(_healthy_snapshot())
    assert reason is None and condor is not None
    assert condor.entry_number == 1
    assert condor.put_short < SPOT < condor.call_short          # both OTM
    assert condor.mid_credit >= CFG.min_total_credit            # STK-06
    assert condor.put_short_mid >= CFG.min_short_premium        # STK-05
    assert condor.call_short_mid >= CFG.min_short_premium


# --- every degraded input must skip, never select -----------------------------

def test_stale_snapshot_never_selects():
    condor, reason = _select(_healthy_snapshot(stale=True))
    assert condor is None and reason == "data_unavailable"      # DAT-02


def test_holey_atm_band_never_selects():
    """Strip most marks inside the band => STK-10 completeness fails."""
    snap = _healthy_snapshot()
    keep = set(list(snap.put_side.marks)[:2])
    holey = ChainSide(snap.put_side.strikes_toward_otm,
                      {k: v for k, v in snap.put_side.marks.items() if k in keep})
    snap.put_side = holey
    condor, reason = _select(snap)
    assert condor is None and reason == "incomplete_chain"


def test_credit_gates_reject_a_thin_chain():
    """All mids far below min_short_premium => the walk finds no valid strike."""
    strikes = [D(str(6000 - 5 * i)) for i in range(25)]
    cheap = {int(k): 0.10 for k in strikes}
    calls = [D(str(6000 + 5 * i)) for i in range(25)]
    snap = Snap(_side(strikes, cheap, descending=True),
                _side(calls, {int(k): 0.10 for k in calls}, descending=False),
                tuple(strikes[:25]), tuple(calls[:25]))
    condor, reason = _select(snap)
    assert condor is None and reason in ("no_valid_strikes", "insufficient_credit")


def test_strike_collision_aborts_the_entry():
    """Existing LONGs on the short and both shift targets => STK-09 abort."""
    snap = _healthy_snapshot()
    condor0, _ = _select(snap)
    assert condor0 is not None
    blocked = {condor0.put_short: frozenset({"long"}),
               condor0.put_short - D("5"): frozenset({"long"}),
               condor0.put_short - D("10"): frozenset({"long"})}
    condor, reason = _select(snap, occupancy=blocked)
    assert condor is None and reason == "strike_collision"


# --- snapshot assembly: only valid two-sided quotes become marks --------------

def test_build_sides_treats_zero_bid_and_crossed_books_as_holes():
    symbols = {D("5990"): ("P90", "C90"), D("6010"): ("P10", "C10")}
    quotes = {
        "P90": (D("2.95"), D("3.05")),   # valid
        "C90": (D("0"), D("1.00")),      # zero bid -> hole
        "P10": (D("2.00"), D("1.00")),   # crossed  -> hole
        "C10": (None, None),             # absent   -> hole
    }
    puts, calls, _, _ = build_sides(spot=SPOT, strike_symbols=symbols,
                                    quotes=quotes, band_points=D("120"))
    assert puts.is_marked(D("5990")) and puts.marks[D("5990")].mid == D("3.00")
    assert not calls.is_marked(D("5990"))   # zero bid
    assert not puts.is_marked(D("6010"))    # crossed
    assert not calls.is_marked(D("6010"))   # absent


def test_build_sides_orders_puts_down_and_calls_up_from_spot():
    symbols = {D(str(s)): (f"P{s}", f"C{s}") for s in (5980, 5990, 6000, 6010, 6020)}
    puts, calls, _, _ = build_sides(spot=SPOT, strike_symbols=symbols, quotes={},
                                    band_points=D("120"))
    assert puts.strikes_toward_otm == (D("6000"), D("5990"), D("5980"))
    assert calls.strikes_toward_otm == (D("6000"), D("6010"), D("6020"))


# --- market calendar (DAY-01/02) ----------------------------------------------

@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 7, 8, 9, 29, tzinfo=ET), False),   # pre-open
    (datetime(2026, 7, 8, 9, 30, tzinfo=ET), True),    # the open
    (datetime(2026, 7, 8, 15, 59, tzinfo=ET), True),
    (datetime(2026, 7, 8, 16, 0, tzinfo=ET), False),   # the close
    (datetime(2026, 7, 11, 12, 0, tzinfo=ET), False),  # Saturday
])
def test_market_hours(when, expected):
    assert is_market_open(when) is expected


def test_holiday_and_half_day():
    hol = date(2026, 7, 3)
    assert is_market_open(datetime(2026, 7, 3, 12, 0, tzinfo=ET), holidays=frozenset({hol})) is False
    half = date(2026, 7, 8)
    assert is_market_open(datetime(2026, 7, 8, 14, 0, tzinfo=ET), half_days=frozenset({half})) is False
    assert is_market_open(datetime(2026, 7, 8, 12, 0, tzinfo=ET), half_days=frozenset({half})) is True


# --- gates provider: an unevaluable gate BLOCKS -------------------------------

class _Clock:
    def __init__(self, now): self._now = now
    def now(self): return self._now


def _gates(now, **kw):
    defaults = dict(data_fresh=lambda: _async(True), session_valid=lambda: _async(True),
                    buying_power_ok=lambda: _async(True))
    defaults.update(kw)
    return asyncio.run(LiveMarketGates(clock=_Clock(now), **defaults)())


OPEN_NOW = datetime(2026, 7, 8, 10, 0, tzinfo=ET)


def test_gates_pass_during_rth_with_healthy_providers():
    g = _gates(OPEN_NOW)
    assert g.market_open and not g.market_halted and g.data_fresh
    assert g.session_valid and g.buying_power_ok


def test_gates_block_outside_market_hours():
    g = _gates(datetime(2026, 7, 8, 8, 0, tzinfo=ET))
    assert g.market_open is False


def test_a_provider_that_raises_blocks_rather_than_waves_through():
    def boom():
        raise RuntimeError("probe failed")
    g = _gates(OPEN_NOW, session_valid=boom, buying_power_ok=boom, data_fresh=boom)
    assert g.session_valid is False and g.buying_power_ok is False and g.data_fresh is False


def test_stale_data_blocks():
    g = _gates(OPEN_NOW, data_fresh=lambda: _async(False))
    assert g.data_fresh is False


# --- DXLink symbol format: quotes are keyed by the STREAMER symbol, not OCC -----
# The bug: snapshot_chain subscribed to DXLink using OCC symbols (s.put), so the
# streamer silently returned NO quotes — indistinguishable from "no market data".
# Found only by driving a live production chain. These pin the symbol choice so it
# cannot regress without touching the network.

def test_streamer_pair_uses_the_dxfeed_streamer_symbol_not_occ():
    from types import SimpleNamespace
    from meic.adapters.dxlink.chain_snapshot import streamer_pair, occ_pair

    strike = SimpleNamespace(
        strike_price=7315.0,
        put="SPXW  260709P07315000",  call="SPXW  260709C07315000",       # OCC
        put_streamer_symbol=".SPXW260709P7315", call_streamer_symbol=".SPXW260709C7315")

    # DXLink subscription/quote-matching MUST use the streamer symbols
    assert streamer_pair(strike) == (".SPXW260709P7315", ".SPXW260709C7315")
    assert all(s.startswith(".") for s in streamer_pair(strike))   # dxfeed form, never OCC
    # ...while ORDERS keep the OCC symbols (the ACL/broker speak OCC)
    assert occ_pair(strike) == ("SPXW  260709P07315000", "SPXW  260709C07315000")


def test_build_sides_matches_quotes_that_arrive_by_streamer_symbol():
    """End of the chain: a quote keyed by the streamer symbol resolves to a mark."""
    from meic.adapters.dxlink.chain_snapshot import build_sides

    strike_symbols = {D("7300"): (".SPXW260709P7300", ".SPXW260709C7300")}
    quotes = {".SPXW260709P7300": (D("3.00"), D("3.10")),   # streamer-keyed, as DXLink sends
              ".SPXW260709C7300": (D("2.50"), D("2.60"))}
    puts, calls, _, _ = build_sides(spot=D("7300"), strike_symbols=strike_symbols,
                                    quotes=quotes, band_points=D("120"))
    assert puts.marks[D("7300")].bid == D("3.00")
    assert calls.marks[D("7300")].ask == D("2.60")
