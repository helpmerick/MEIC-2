"""TPF/TPT health-tick evaluation (server.py `_open_side_costs`,
`_entry_profit_pct_now`, `_evaluate_exits_once`, `_recover_exits_once`) —
unit-tested against a fake chain snapshot, mirroring
`tests/adapters/test_live_pnl_enricher.py`'s style (no DXLink, no broker).
"""
import asyncio
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

PUT_SHORT_SYM, PUT_LONG_SYM = "SPXW260709P07535000", "SPXW260709P07510000"
CALL_SHORT_SYM, CALL_LONG_SYM = "SPXW260709C07540000", "SPXW260709C07565000"


def _legs():
    return (
        FilledLeg(symbol=PUT_SHORT_SYM, right="P", role="short", qty=1, price=D("1.80")),
        FilledLeg(symbol=PUT_LONG_SYM, right="P", role="long", qty=1, price=D("0.08")),
        FilledLeg(symbol=CALL_SHORT_SYM, right="C", role="short", qty=1, price=D("1.95")),
        FilledLeg(symbol=CALL_LONG_SYM, right="C", role="long", qty=1, price=D("0.07")),
    )


def _snapshot(put_marks: dict, call_marks: dict, *, stale: bool = False) -> ChainSnapshot:
    return ChainSnapshot(
        spot=D("7540"), expiration=None,
        put_side=ChainSide(strikes_toward_otm=tuple(sorted(put_marks, reverse=True)), marks=put_marks),
        call_side=ChainSide(strikes_toward_otm=tuple(sorted(call_marks)), marks=call_marks),
        put_band=(), call_band=(), symbols={},
        taken_at=None, stale=stale)


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
