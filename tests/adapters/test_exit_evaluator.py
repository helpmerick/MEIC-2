"""TPF/TPT health-tick evaluation (server.py `_open_side_costs`,
`_entry_profit_pct_now`, `_evaluate_exits_once`, `_recover_exits_once`) —
unit-tested against a fake chain snapshot, mirroring
`tests/adapters/test_live_pnl_enricher.py`'s style (no DXLink, no broker).
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from meic.adapters.api.server import (
    _entry_profit_pct_now,
    _evaluate_exits_once,
    _open_side_costs,
    _profit_pct_enricher,
    _recover_exits_once,
)
from meic.adapters.dxlink.chain_snapshot import ChainSnapshot
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.exit_monitor import ExitMonitor
from meic.application.persistent_state import PersistentState
from meic.domain.chain import ChainSide, Mark
from meic.domain.events import CondorFilled, FilledLeg, ShortStopped
from meic.domain.projection import fold
from meic.domain.staleness import StampedQuote

PUT_SHORT_SYM, PUT_LONG_SYM = "SPXW260709P07535000", "SPXW260709P07510000"
CALL_SHORT_SYM, CALL_LONG_SYM = "SPXW260709C07540000", "SPXW260709C07565000"

# NFR-04: the DXFEED STREAMER symbols for the SAME strikes -- the only namespace
# DXLink sends, and therefore the namespace the QuoteHub is keyed by. The legs
# above carry OCC; the evaluator must translate via
# `ChainSnapshot.streamer_symbols` before any hub lookup.
PUT_SHORT_STREAMER, PUT_LONG_STREAMER = ".SPXW260709P7535", ".SPXW260709P7510"
CALL_SHORT_STREAMER, CALL_LONG_STREAMER = ".SPXW260709C7540", ".SPXW260709C7565"

STREAMER_MAP = {
    D("7535"): (PUT_SHORT_STREAMER, ".SPXW260709C7535"),
    D("7510"): (PUT_LONG_STREAMER, ".SPXW260709C7510"),
    D("7540"): (".SPXW260709P7540", CALL_SHORT_STREAMER),
    D("7565"): (".SPXW260709P7565", CALL_LONG_STREAMER),
}


def _legs():
    return (
        FilledLeg(symbol=PUT_SHORT_SYM, right="P", role="short", qty=1, price=D("1.80")),
        FilledLeg(symbol=PUT_LONG_SYM, right="P", role="long", qty=1, price=D("0.08")),
        FilledLeg(symbol=CALL_SHORT_SYM, right="C", role="short", qty=1, price=D("1.95")),
        FilledLeg(symbol=CALL_LONG_SYM, right="C", role="long", qty=1, price=D("0.07")),
    )


def _snapshot(put_marks: dict, call_marks: dict, *, stale: bool = False,
              streamer_symbols: dict | None = None) -> ChainSnapshot:
    return ChainSnapshot(
        spot=D("7540"), expiration=None,
        put_side=ChainSide(strikes_toward_otm=tuple(sorted(put_marks, reverse=True)), marks=put_marks),
        call_side=ChainSide(strikes_toward_otm=tuple(sorted(call_marks)), marks=call_marks),
        put_band=(), call_band=(), symbols={},
        taken_at=None, stale=stale,
        streamer_symbols=STREAMER_MAP if streamer_symbols is None else streamer_symbols)


FULL_MARKS = (
    {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")), D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))},
    {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")), D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))},
)


class _Comp:
    def __init__(self, events=None, floors=None, targets=None):
        self.events = list(events or [])
        self.state = PersistentState(InMemoryStateStore())
        self.state.tpf_floors = floors or {}
        self.state.tp_targets = targets or {}


class _Snaps:
    def __init__(self, last=None):
        self.last = last


class _Commands:
    def __init__(self):
        self.closed: list[tuple[str, str]] = []

    async def close_as(self, entry_id, initiator):
        self.closed.append((entry_id, initiator))


class _FakeHub:
    """NFR-04: minimal stand-in for `QuoteHub.mark` -- a symbol -> StampedQuote map,
    mirroring `tests/adapters/test_live_pnl_enricher.py`'s fake."""

    def __init__(self, marks: dict[str, StampedQuote] | None = None):
        self._marks = marks or {}

    def mark(self, symbol):
        return self._marks.get(symbol)


class _FakeClock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now


# --- _open_side_costs / _entry_profit_pct_now -------------------------------

def test_open_side_costs_both_sides_open():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    e = fold(events).entries["e1"]
    snap = _snapshot(*FULL_MARKS)
    costs = _open_side_costs(e, snap)
    # PUT: 1.70 - 0.08 = 1.62; CALL: 1.95 - 0.07 = 1.88
    assert costs == {"PUT": D("1.62"), "CALL": D("1.88")}
    pct = _entry_profit_pct_now(e, snap)
    # profit = 3.60 - 1.62 - 1.88 = 0.10; pct = 0.10/3.60*100
    assert pct == D("0.10") / D("3.60") * 100


def test_open_side_costs_none_when_a_mark_is_missing():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    e = fold(events).entries["e1"]
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75"))}  # 7510 missing
    snap = _snapshot(put_marks, FULL_MARKS[1])
    assert _open_side_costs(e, snap) is None
    assert _entry_profit_pct_now(e, snap) is None


def test_stopped_side_excluded_from_open_costs():
    """TPF-05: a stopped side contributes its REALIZED effect only — it is
    never re-marked."""
    events = [
        CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs()),
        ShortStopped(entry_id="e1", side="PUT", fill=D("3.80"), slippage=D("0"), initiator="resting_stop"),
    ]
    e = fold(events).entries["e1"]
    snap = _snapshot(*FULL_MARKS)
    costs = _open_side_costs(e, snap)
    assert costs == {"CALL": D("1.88")}   # PUT excluded — already stopped


# --- _evaluate_exits_once: floor ---------------------------------------------

def test_floor_fires_after_confirmation_evals_via_close_as():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    # deep in-the-money-for-the-condor marks -> big open cost -> low/negative profit%
    put_marks = {D("7535"): Mark(bid=D("3.00"), ask=D("3.10")), D("7510"): Mark(bid=D("0.02"), ask=D("0.03"))}
    call_marks = {D("7540"): Mark(bid=D("0.05"), ask=D("0.06")), D("7565"): Mark(bid=D("0.01"), ask=D("0.02"))}
    comp = _Comp(events, floors={"e1": 90})   # a floor no realistic profit clears
    snap = _snapshot(put_marks, call_marks)
    monitor = ExitMonitor()
    commands = _Commands()

    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands))
    assert commands.closed == []   # 1st confirmation only
    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands))
    assert commands.closed == [("e1", "take_profit")]   # 2nd confirmation fires


def test_stale_snapshot_pauses_evaluation_never_fires():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    comp = _Comp(events, floors={"e1": 90})
    snap = _snapshot(*FULL_MARKS, stale=True)
    monitor = ExitMonitor()
    commands = _Commands()

    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands))
    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands))
    assert commands.closed == []


# --- TPT-05: permanent disarm on any stop -------------------------------------

def test_target_disarms_permanently_when_a_stop_fills():
    """Pinned vector (TC-TPT-01): credit 4.00, target 5%, put stops at 3.80,
    long recovers 0.30, call closable for 0.20 -> whole-entry profit +$30 =
    7.5% >= 5% target -- and NOTHING fires; the target died with the stop."""
    from meic.domain.events import LongSold

    legs = (
        FilledLeg(symbol=PUT_SHORT_SYM, right="P", role="short", qty=1, price=D("2.00")),
        FilledLeg(symbol=PUT_LONG_SYM, right="P", role="long", qty=1, price=D("0.00")),
        FilledLeg(symbol=CALL_SHORT_SYM, right="C", role="short", qty=1, price=D("2.00")),
        FilledLeg(symbol=CALL_LONG_SYM, right="C", role="long", qty=1, price=D("0.00")),
    )
    events = [
        CondorFilled(entry_id="e1", net_credit=D("4.00"), legs=legs),
        ShortStopped(entry_id="e1", side="PUT", fill=D("3.80"), slippage=D("0"), initiator="resting_stop"),
        LongSold(entry_id="e1", side="PUT", recovery=D("0.30"), fee=D("0")),
    ]
    call_marks = {D("7540"): Mark(bid=D("0.19"), ask=D("0.21")), D("7565"): Mark(bid=D("0.00"), ask=D("0.00"))}
    comp = _Comp(events, targets={"e1": 5})
    snap = _snapshot({}, call_marks)   # PUT already stopped -- no PUT marks needed
    monitor = ExitMonitor()
    commands = _Commands()

    e = fold(events).entries["e1"]
    assert e.sides_stopped == ("PUT",)
    pct = _entry_profit_pct_now(e, snap)
    assert pct == pytest.approx(D("30") / D("400") * 100, rel=D("0.001"))  # 7.5%

    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands))
    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands))
    assert commands.closed == []   # disarmed -- never fires despite profit >= target


# --- TPF-08/TPT-07: immediate recovery fire ----------------------------------

def test_recovery_fires_an_already_breached_floor_immediately_no_confirmation_wait():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    put_marks = {D("7535"): Mark(bid=D("3.00"), ask=D("3.10")), D("7510"): Mark(bid=D("0.02"), ask=D("0.03"))}
    call_marks = {D("7540"): Mark(bid=D("0.05"), ask=D("0.06")), D("7565"): Mark(bid=D("0.01"), ask=D("0.02"))}
    comp = _Comp(events, floors={"e1": 90})
    snap = _snapshot(put_marks, call_marks)
    commands = _Commands()

    asyncio.run(_recover_exits_once(comp, snap, commands))
    assert commands.closed == [("e1", "take_profit")]   # fires on the FIRST call


# --- UI-13/14/15: /entries profit_pct enricher --------------------------------

def test_profit_pct_enricher_reports_the_shared_evaluator_result():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    comp = _Comp(events)
    snap = _snapshot(*FULL_MARKS)
    enrich = _profit_pct_enricher(comp, _Snaps(snap))

    cards = enrich([{"entry_id": "e1"}])

    assert cards[0]["profit_pct"] == str(D("0.10") / D("3.60") * 100)


def test_profit_pct_enricher_is_null_with_no_snapshot():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    comp = _Comp(events)
    enrich = _profit_pct_enricher(comp, _Snaps(None))

    cards = enrich([{"entry_id": "e1"}])

    assert cards[0]["profit_pct"] is None


def test_recovery_respects_disarm_order_synthesized_stop_processed_first():
    """TPT-07: a synthesized stop event (already in the log by the time
    `_recover_exits_once` is called, per `_boot_reconcile`'s ordering) disarms
    the target BEFORE this recovery check runs."""
    legs = (
        FilledLeg(symbol=PUT_SHORT_SYM, right="P", role="short", qty=1, price=D("2.00")),
        FilledLeg(symbol=PUT_LONG_SYM, right="P", role="long", qty=1, price=D("0.00")),
        FilledLeg(symbol=CALL_SHORT_SYM, right="C", role="short", qty=1, price=D("2.00")),
        FilledLeg(symbol=CALL_LONG_SYM, right="C", role="long", qty=1, price=D("0.00")),
    )
    events = [
        CondorFilled(entry_id="e1", net_credit=D("4.00"), legs=legs),
        ShortStopped(entry_id="e1", side="PUT", fill=D("3.80"), slippage=D("0"), initiator="resting_stop"),
    ]
    call_marks = {D("7540"): Mark(bid=D("0.19"), ask=D("0.21")), D("7565"): Mark(bid=D("0.00"), ask=D("0.00"))}
    comp = _Comp(events, targets={"e1": 5})
    snap = _snapshot({}, call_marks)
    commands = _Commands()

    asyncio.run(_recover_exits_once(comp, snap, commands))
    assert commands.closed == []   # disarmed before the check ever ran


# --- NFR-04: QuoteHub live-first / snapshot-fallback resolution -------------
# `test_open_side_costs_both_sides_open` above is the pinned "strictly no
# worse than today" baseline every test below is checked against: costs
# {"PUT": 1.62, "CALL": 1.88}, pct = 0.10/3.60*100.
#
# Every hub below is keyed by STREAMER symbol, as the live one is, while the
# legs carry OCC -- so a lookup that skips the translation finds nothing (the
# always-empty-hub bug found live on 2026-07-13).

_NOW = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)

# junk snapshot marks -- if the snapshot were ever preferred over a fresh hub
# tick, the totals below would be visibly wrong.
_JUNK_PUT = {D("7535"): Mark(bid=D("9.00"), ask=D("9.10")), D("7510"): Mark(bid=D("9.00"), ask=D("9.10"))}
_JUNK_CALL = {D("7540"): Mark(bid=D("9.00"), ask=D("9.10")), D("7565"): Mark(bid=D("9.00"), ask=D("9.10"))}


def _streamer_keyed_hub(at, *, put_short=("1.65", "1.75"), put_long=("0.07", "0.09"),
                        call_short=("1.90", "2.00"), call_long=("0.06", "0.08")) -> _FakeHub:
    return _FakeHub({
        PUT_SHORT_STREAMER: StampedQuote(PUT_SHORT_STREAMER, D(put_short[0]), D(put_short[1]), at),
        PUT_LONG_STREAMER: StampedQuote(PUT_LONG_STREAMER, D(put_long[0]), D(put_long[1]), at),
        CALL_SHORT_STREAMER: StampedQuote(CALL_SHORT_STREAMER, D(call_short[0]), D(call_short[1]), at),
        CALL_LONG_STREAMER: StampedQuote(CALL_LONG_STREAMER, D(call_long[0]), D(call_long[1]), at),
    })


def test_nfr04_open_side_costs_prefers_a_fresh_hub_mark():
    """A fresh hub mark for EVERY leg is used instead of the (here,
    deliberately junk) snapshot marks -- proving the hub value actually wins,
    and that the OCC->streamer translation actually finds it."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    e = fold(events).entries["e1"]
    snap = _snapshot(_JUNK_PUT, _JUNK_CALL)
    hub = _streamer_keyed_hub(_NOW)

    costs = _open_side_costs(e, snap, hub=hub, now=_NOW, max_quote_age_ms=3000)

    assert costs == {"PUT": D("1.62"), "CALL": D("1.88")}   # the HUB's marks, not the junk snapshot's


def test_nfr04_open_side_costs_ignores_a_hub_keyed_by_occ_symbol():
    """The namespace guard on the TPF/TPT path: a hub holding marks ONLY under
    the OCC strings (i.e. what the buggy first cut would have written) must be
    treated as EMPTY -- the evaluator falls back to the snapshot rather than
    reading them."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    e = fold(events).entries["e1"]
    snap = _snapshot(*FULL_MARKS)
    occ_keyed = _FakeHub({
        PUT_SHORT_SYM: StampedQuote(PUT_SHORT_SYM, D("9.00"), D("9.10"), _NOW),
        PUT_LONG_SYM: StampedQuote(PUT_LONG_SYM, D("9.00"), D("9.10"), _NOW),
        CALL_SHORT_SYM: StampedQuote(CALL_SHORT_SYM, D("9.00"), D("9.10"), _NOW),
        CALL_LONG_SYM: StampedQuote(CALL_LONG_SYM, D("9.00"), D("9.10"), _NOW),
    })

    costs = _open_side_costs(e, snap, hub=occ_keyed, now=_NOW, max_quote_age_ms=3000)

    assert costs == {"PUT": D("1.62"), "CALL": D("1.88")}   # snapshot values -- the OCC junk is never read


def test_nfr04_open_side_costs_strike_absent_from_streamer_map_falls_back():
    """A leg outside the subscribed span has no streamer symbol -- never
    guessed, never looked up; it resolves off the snapshot with no crash."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    e = fold(events).entries["e1"]
    partial = {k: v for k, v in STREAMER_MAP.items() if k != D("7510")}   # put long dropped
    # put long resolves off the snapshot (0.08); the rest come off the hub.
    snap = _snapshot(FULL_MARKS[0], _JUNK_CALL, streamer_symbols=partial)
    hub = _streamer_keyed_hub(_NOW)

    costs = _open_side_costs(e, snap, hub=hub, now=_NOW, max_quote_age_ms=3000)

    assert costs == {"PUT": D("1.62"), "CALL": D("1.88")}


def test_nfr04_open_side_costs_stale_hub_mark_falls_back_to_snapshot():
    """A hub mark older than `max_quote_age_ms` is ABSENT -- falls through to
    the exact snapshot value, reproducing the pinned baseline."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    e = fold(events).entries["e1"]
    snap = _snapshot(*FULL_MARKS)
    stale_at = _NOW - timedelta(milliseconds=4000)   # older than the 3000ms bar
    hub = _streamer_keyed_hub(stale_at, put_short=("9.00", "9.10"), put_long=("9.00", "9.10"),
                              call_short=("9.00", "9.10"), call_long=("9.00", "9.10"))

    costs = _open_side_costs(e, snap, hub=hub, now=_NOW, max_quote_age_ms=3000)

    assert costs == {"PUT": D("1.62"), "CALL": D("1.88")}   # snapshot values, matching the pinned baseline


def test_nfr04_open_side_costs_empty_hub_is_byte_identical_to_pre_wiring():
    """STRICTLY NO WORSE proof: an empty hub reproduces the exact pinned
    baseline from `test_open_side_costs_both_sides_open` -- no regression."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    e = fold(events).entries["e1"]
    snap = _snapshot(*FULL_MARKS)
    hub = _FakeHub({})

    costs = _open_side_costs(e, snap, hub=hub, now=_NOW, max_quote_age_ms=3000)

    assert costs == {"PUT": D("1.62"), "CALL": D("1.88")}


def test_nfr04_entry_profit_pct_now_prefers_live_marks():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    e = fold(events).entries["e1"]
    snap = _snapshot(_JUNK_PUT, _JUNK_CALL)
    hub = _streamer_keyed_hub(_NOW)

    pct = _entry_profit_pct_now(e, snap, hub=hub, now=_NOW, max_quote_age_ms=3000)

    assert pct == D("0.10") / D("3.60") * 100   # the pinned baseline pct, off the HUB's marks


def test_nfr04_profit_pct_enricher_uses_hub_when_fresh():
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    comp = _Comp(events)
    snap = _snapshot(_JUNK_PUT, _JUNK_CALL)
    hub = _streamer_keyed_hub(_NOW)
    enrich = _profit_pct_enricher(comp, _Snaps(snap), hub, clock=_FakeClock(_NOW), max_quote_age_ms=3000)

    cards = enrich([{"entry_id": "e1"}])

    assert cards[0]["profit_pct"] == str(D("0.10") / D("3.60") * 100)


def test_nfr04_profit_pct_enricher_empty_hub_is_byte_identical_to_pre_wiring():
    """STRICTLY NO WORSE proof, mirroring
    `test_profit_pct_enricher_reports_the_shared_evaluator_result` exactly."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    comp = _Comp(events)
    snap = _snapshot(*FULL_MARKS)
    hub = _FakeHub({})
    enrich = _profit_pct_enricher(comp, _Snaps(snap), hub, clock=_FakeClock(_NOW), max_quote_age_ms=3000)

    cards = enrich([{"entry_id": "e1"}])

    assert cards[0]["profit_pct"] == str(D("0.10") / D("3.60") * 100)


def test_nfr04_evaluate_exits_once_fires_off_a_live_hub_mark():
    """TPF/TPT (`_evaluate_exits_once`) gets the SAME hub-aware resolution --
    deep-ITM marks living ONLY in the hub (the snapshot is clean/at-the-money
    and would never breach the floor on its own) still breach a 90% floor."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    comp = _Comp(events, floors={"e1": 90})
    snap = _snapshot(*FULL_MARKS)   # healthy marks -- would NOT breach a 90% floor alone
    hub = _streamer_keyed_hub(_NOW, put_short=("3.00", "3.10"), put_long=("0.02", "0.03"),
                              call_short=("0.05", "0.06"), call_long=("0.01", "0.02"))  # deep ITM, LIVE
    monitor = ExitMonitor()
    commands = _Commands()

    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands, hub=hub, clock=_FakeClock(_NOW)))
    assert commands.closed == []   # 1st confirmation only
    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands, hub=hub, clock=_FakeClock(_NOW)))
    assert commands.closed == [("e1", "take_profit")]   # 2nd confirmation fires, off the LIVE marks


def test_nfr04_evaluate_exits_once_empty_hub_is_byte_identical_to_pre_wiring():
    """STRICTLY NO WORSE proof, mirroring
    `test_floor_fires_after_confirmation_evals_via_close_as` exactly."""
    events = [CondorFilled(entry_id="e1", net_credit=D("3.60"), legs=_legs())]
    put_marks = {D("7535"): Mark(bid=D("3.00"), ask=D("3.10")), D("7510"): Mark(bid=D("0.02"), ask=D("0.03"))}
    call_marks = {D("7540"): Mark(bid=D("0.05"), ask=D("0.06")), D("7565"): Mark(bid=D("0.01"), ask=D("0.02"))}
    comp = _Comp(events, floors={"e1": 90})
    snap = _snapshot(put_marks, call_marks)
    monitor = ExitMonitor()
    commands = _Commands()
    hub = _FakeHub({})

    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands, hub=hub, clock=_FakeClock(_NOW)))
    assert commands.closed == []
    asyncio.run(_evaluate_exits_once(comp, snap, monitor, commands, hub=hub, clock=_FakeClock(_NOW)))
    assert commands.closed == [("e1", "take_profit")]
