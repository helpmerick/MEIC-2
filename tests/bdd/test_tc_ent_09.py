"""Hand-written step definitions for TC-ENT-09 — ENT-09b v1.57 manual minimum
short-strike floors (spec/01-strategy-rules.md ENT-09b; spec/04-test-cases.md
TC-ENT-09).

FLOOR semantics, never a pin: puts select short <= the floor, calls select
short >= the floor — `domain/walk.py: select_side`'s `short_floor=` parameter,
filtering candidates so "the probe walk runs unchanged among qualifying
strikes". Credit rules (STK-05/06) are untouched: a floor can only ever cause
`no_valid_strikes`, never weaken a gate. Refuse-and-re-pick
(`domain/walk.py: floor_inside_spot`) lives in `application/manual_entry.py`'s
`fire()`. Dropdown candidates are the v1.55 VALIDATED UNIVERSE
(`composition/live_selection.py: floor_candidates`). Floors are recorded on
`EntrySkipped`/`CondorFilled` (domain/events.py).
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.clocks import MutableClock
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.manual_entry import ManualEntry
from meic.application.persistent_state import PersistentState
from meic.composition.live_selection import SelectionConfig, floor_candidates
from meic.domain.chain import ChainSide, Mark
from meic.domain.events import CondorFilled
from meic.domain.projection import fold
from meic.domain.ticks import TickRung, TickTable
from meic.domain.walk import Selected, Skip, floor_inside_spot, select_side
from tests.harness.fake_broker import FakeBroker

scenarios("../features/TC-ENT-09.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
IS_CONDOR = lambda o: o.kind == "iron_condor"


def _mk(mid: str) -> Mark:
    m = D(mid)
    return Mark(bid=m - D("0.05"), ask=m + D("0.05"))


async def _gates_ok() -> GateSnapshot:
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


class _Snap:
    def __init__(self, put_side, call_side, spot, stale=False, taken_at=None):
        self.put_side, self.call_side, self.spot, self.stale = put_side, call_side, spot, stale
        self.taken_at = taken_at


@pytest.fixture
def world():
    return {}


# --- Scenario: A floor filters the walk without changing it ------------------

@given('SPX at 7480 and a manual fire with put floor 7450')
def _(world):
    world["spot"] = D("7480")
    world["put_floor"] = D("7450")


@given('the probe walk would normally match the 7460 put')
def _(world):
    # puts descending from spot; 7460 (2 strikes OTM) sits exactly at the
    # 3.00 target -- the walk's normal (unfloored) match. 7450 (the floor)
    # is priced 2.95 -- the NEXT probe in the deterministic sequence
    # (T, T-0.05, T+0.05, ...), so once 7460 is excluded the walk lands
    # exactly ON the floor, never guessing past it.
    strikes = (D("7480"), D("7475"), D("7470"), D("7465"), D("7460"),
              D("7455"), D("7450"), D("7445"), D("7440"), D("7410"), D("7400"))
    marks = {
        D("7480"): _mk("3.60"), D("7475"): _mk("3.30"), D("7470"): _mk("3.15"),
        D("7465"): _mk("3.05"), D("7460"): _mk("3.00"), D("7455"): _mk("2.98"),
        D("7450"): _mk("2.95"), D("7445"): _mk("2.20"), D("7440"): _mk("1.50"),
        D("7410"): _mk("0.50"), D("7400"): _mk("0.30"),   # the two candidates' wings (width 50)
    }
    side = ChainSide(strikes, marks)
    unfloored = select_side(side, target_premium=D("3.00"), wing_width=D("50"),
                            otm_direction=D(-1), min_short_premium=D("1.00"))
    assert isinstance(unfloored, Selected) and unfloored.short_strike == D("7460")
    world["side"] = side


@then('strikes inside the floor are excluded and the walk selects at or beyond 7450')
def _(world):
    floored = select_side(world["side"], target_premium=D("3.00"), wing_width=D("50"),
                          otm_direction=D(-1), min_short_premium=D("1.00"),
                          short_floor=world["put_floor"])
    assert isinstance(floored, Selected)
    assert floored.short_strike == D("7450")           # AT the floor -- inclusive, never beyond
    assert floored.short_strike <= world["put_floor"]


@then('the call side runs default behaviour when no call floor is set')
def _(world):
    call_strikes = tuple(D("7480") + D(5 * i) for i in range(15))
    call_marks = {s: _mk(str(max(D("0.15"), D("3.60") - D("0.30") * i)))
                 for i, s in enumerate(call_strikes)}
    call_side = ChainSide(call_strikes, call_marks)
    result = select_side(call_side, target_premium=D("3.00"), wing_width=D("50"),
                         otm_direction=D(1), min_short_premium=D("1.00"),
                         short_floor=None)   # no call floor set
    assert isinstance(result, Selected) and result.short_strike == D("7490")  # unfiltered normal walk


# --- Scenario: Credit rules are never weakened by a floor ---------------------

@given('floors that leave no strike satisfying 1.00 gross and 2.00 total net')
def _(world):
    # a put floor of 7480 (== spot): puts must select AT or BELOW the floor
    # to qualify, but every listed strike here sits ABOVE the target's probe
    # window match (7480/7475/7470 all round to 3.60/3.30/3.15 -- only 7470's
    # 3.15 is within probe_up_max=3's ceiling of target+0.15=3.15, and 7470
    # itself is BELOW the 7480 floor... engineered so NOTHING both qualifies
    # the floor AND matches a probe: the floor can only ever cause a skip.
    strikes = (D("7480"), D("7475"), D("7470"))
    marks = {D("7480"): _mk("3.60"), D("7475"): _mk("3.30"), D("7470"): _mk("0.50")}
    world["side"] = ChainSide(strikes, marks)
    world["floor"] = D("7480")


@then('the fire skips with reason "no_valid_strikes" and no order is placed')
def _(world):
    result = select_side(world["side"], target_premium=D("3.00"), wing_width=D("50"),
                         otm_direction=D(-1), min_short_premium=D("1.00"),
                         short_floor=world["floor"])
    # 7480 is the only floor-qualifying strike (puts select short <= 7480);
    # its mid (3.60) never matches any probe in range -- no_valid_strikes,
    # never a guessed/weakened credit outcome.
    assert isinstance(result, Skip) and result.reason == "no_valid_strikes"


# --- Scenario: Refuse and re-pick when spot crosses a floor -------------------

@given('the dialog opened with SPX 7480 and call floor 7500 selected')
def _(world):
    world["call_floor"] = D("7500")
    assert floor_inside_spot(D("7480"), put_floor=None, call_floor=D("7500")) is False


@when('SPX is 7505 at OK time')
def _(world):
    world["spot_at_ok"] = D("7505")


@then('the fire is REFUSED with reason "floor_inside_spot"')
def _(world):
    assert floor_inside_spot(world["spot_at_ok"], put_floor=None,
                             call_floor=world["call_floor"]) is True

    # end-to-end through ManualEntry.fire(): the refusal happens BEFORE the
    # selector ever runs -- no selection, no order, no ambiguity.
    class _Comp:
        def __init__(self):
            self.events: list = []
            self.clock = MutableClock(datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc))
            self.state = PersistentState(InMemoryStateStore())
            self.state.armed = True
            self.state.confirm_live = True
            self.state.stop_trading = False

    comp = _Comp()
    selector_calls = {"n": 0}

    async def selector(when, n, config=None, put_floor=None, call_floor=None):
        selector_calls["n"] += 1
        return Condor(entry_number=n, put_short=D("7460"), call_short=D("7500"),
                     put_long=D("7410"), call_long=D("7550"),
                     put_short_mid=D("3.00"), call_short_mid=D("3.00"),
                     mid_credit=D("4.00"), min_total_credit=D("2.00"),
                     expiration=None, contracts=1), None

    manual = ManualEntry(comp, selector, _gates_ok, day=lambda: "2026-07-10",
                        spot_provider=lambda: world["spot_at_ok"])
    result = asyncio.run(manual.fire(press_id="p1", entry_number=1, row=None,
                                     confirmed=True, call_floor=world["call_floor"]))
    world["comp"], world["refuse_result"] = comp, result
    assert selector_calls["n"] == 0   # refused BEFORE selection ever ran
    assert result == {"result": "skipped", "reason": "floor_inside_spot"}


@then('the operator must re-select before any order can be placed')
def _(world):
    from meic.domain.events import EntrySkipped

    assert world["refuse_result"]["reason"] == "floor_inside_spot"
    skips = [e for e in world["comp"].events if isinstance(e, EntrySkipped)]
    assert len(skips) == 1 and skips[0].reason == "floor_inside_spot"
    assert skips[0].call_floor == D("7500")   # audited on the skip too


# --- Scenario: Dropdowns come from the validated universe only ---------------

@then('every selectable strike has fresh two-sided quotes at dialog population')
def _(world):
    strikes = tuple(D("7480") - D(5 * i) for i in range(10))
    marks = {s: _mk(str(max(D("0.15"), D("3.60") - D("0.30") * i)))
            for i, s in enumerate(strikes) if i != 3}   # 7465 (i=3) has NO mark -- a hole
    put_side = ChainSide(strikes, marks)
    call_strikes = tuple(D("7480") + D(5 * i) for i in range(10))
    call_marks = {s: _mk(str(max(D("0.15"), D("3.60") - D("0.30") * i)))
                 for i, s in enumerate(call_strikes)}
    call_side = ChainSide(call_strikes, call_marks)

    snap = _Snap(put_side, call_side, spot=D("7480"))
    cfg = SelectionConfig(target_premium=D("3.00"), wing_width=D("50"),
                         min_short_premium=D("1.00"))
    result = floor_candidates(snap, cfg)
    world["candidates"] = result

    put_strikes_shown = {D(r["strike"]) for r in result["put"]}
    assert D("7465") not in put_strikes_shown   # the hole never appears as selectable
    assert put_strikes_shown   # but real candidates do


@then('each row shows strike, points from spot, and live mid')
def _(world):
    for side in ("put", "call"):
        for row in world["candidates"][side]:
            assert set(row) == {"strike", "distance_pts", "mid"}
    assert world["candidates"]["spot"] == "7480"


# --- Scenario: Floors are evented for audit -----------------------------------

@given('a manual fire with put floor 7450 and no call floor')
def _(world):
    world["put_floor"] = D("7450")
    world["call_floor"] = None


@then('the entry events record the floors and the day report shows them')
def _(world):
    events: list = []
    broker = FakeBroker()
    broker.autofill(IS_CONDOR)
    clock = MutableClock(datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc))
    execute = ExecuteEntryAttempt(broker, clock, events, SPX)

    condor = Condor(entry_number=1, put_short=D("7450"), call_short=D("7550"),
                    put_long=D("7400"), call_long=D("7600"),
                    put_short_mid=D("3.00"), call_short_mid=D("3.00"),
                    mid_credit=D("4.00"), min_total_credit=D("2.00"),
                    expiration=None, contracts=1)

    outcome = asyncio.run(execute.attempt(
        day="2026-07-10", scheduled=clock.now(), condor=condor,
        gates=asyncio.run(_gates_ok()), bypass_window=True, initiator="manual_entry",
        put_floor=world["put_floor"], call_floor=world["call_floor"]))
    assert outcome.status == "FILLED"

    fills = [e for e in events if isinstance(e, CondorFilled)]
    assert fills and fills[0].put_floor == D("7450") and fills[0].call_floor is None

    state = fold(events)
    proj = state.entries["2026-07-10#1"]
    assert proj.put_floor == D("7450") and proj.call_floor is None
