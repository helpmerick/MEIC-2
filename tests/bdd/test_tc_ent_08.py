"""TC-ENT-08 — ENT-09 manual entry (▶) + UI-22 confirmation dialog.

The manual fire bypasses the ENT-02 window and NOTHING else: it runs through the
identical ExecuteEntryAttempt.attempt(), so the ENT-03 chain, RSK-08 and RSK-04
all apply. Confirmation is a simple OK dialog (operator-ratified: not typed) in
both paper and live. Idempotent per press. Recorded with initiator manual_entry.
"""
import asyncio
from datetime import date, datetime, time, timezone
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.entry_gates import GateSnapshot, RiskSnapshot
from meic.application.event_log import EventLog
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.manual_entry import MANUAL, ManualEntry
from meic.application.persistent_state import PersistentState
from meic.domain.events import CondorFilled, EntrySkipped, EntryWindowOpened
from meic.domain.schedule import ResolvedEntry
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker

scenarios("../features/TC-ENT-08.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
# 10:07 — deliberately not any scheduled entry time
NOW = datetime(2026, 7, 6, 10, 7, tzinfo=timezone.utc)
SCHEDULED = datetime(2026, 7, 6, 9, 32, tzinfo=timezone.utc)   # long past
IS_CONDOR = lambda o: o.kind == "iron_condor"


def _row(**over) -> ResolvedEntry:
    base = dict(time=time(10, 0), contracts=1, target_premium=D("3.00"), wing_width=D("50"),
                stop_loss_pct=95, stop_basis="total_credit", stop_rebate_markup=D("0.00"),
                min_short_premium=D("1.00"), min_total_credit=D("2.00"), probe_down_max=25,
                strike_method="premium", short_delta_target=D("0.10"))
    return ResolvedEntry(**{**base, **over})


def _condor(n=1, contracts=1):
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5940"), call_long=D("6110"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 7, 6), contracts=contracts)


class _Clock:
    def now(self):
        return NOW

    async def wait_until(self, when):
        return None


class _Comp:
    def __init__(self, *, stop_trading=False):
        self.broker = FakeBroker()
        self.broker.autofill(IS_CONDOR)
        self.events = EventLog(config_version="v1.46")
        self.clock = _Clock()
        self.execute = ExecuteEntryAttempt(self.broker, self.clock, self.events, SPX)
        self.state = PersistentState(InMemoryStateStore())
        self.state.armed = True
        self.state.confirm_live = True
        self.state.stop_trading = stop_trading
        self.protected: list = []

    async def _on_filled(self, entry_id, condor, stop=None, fill_credit=None):
        self.protected.append(entry_id)


async def _gates():
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


def _manual(comp, *, risk=None, max_entries=None):
    async def selector(when, n, config=None, put_floor=None, call_floor=None):
        return _condor(n, config.contracts if config else 1), None

    return ManualEntry(comp, selector, _gates, max_entries_per_day=max_entries,
                       risk=(lambda: risk) if risk else None, day=lambda: "2026-07-06")


def _submitted(comp):
    return [o.intent for o in comp.broker._orders.values() if o.intent.kind == "iron_condor"]


@pytest.fixture
def world():
    return {}


# --- Scenario 1: manual fire passes every gate except the window ------------------

@given('the operator presses the manual fire button at 10:07, outside any scheduled window')
def _(world):
    world["comp"] = _Comp()
    world["press_id"] = "press-1"

    # a SCHEDULED attempt at this moment would be refused: the window is long gone
    scheduled = asyncio.run(world["comp"].execute.attempt(
        day="2026-07-06", scheduled=SCHEDULED, condor=_condor(9),
        gates=asyncio.run(_gates())))
    assert scheduled.reason == "missed_window"
    world["comp"].events.clear()


@given('all ENT-03 gates pass')
def _(world):
    world["manual"] = _manual(world["comp"])


@when('the OK-confirmation dialog is acknowledged')
def _(world):
    manual = world["manual"]
    preview = manual.preview(world["press_id"], 1, _row()).to_dict()
    # UI-22 (v1.46): the dialog shows the row's parameters and a LABELLED estimate
    assert preview["worst_case_is_estimate"] is True
    assert D(preview["worst_case_estimate"]) == D("4700")     # (50 - 3) x 100 x 1
    assert preview["contracts"] == 1 and preview["stop_loss_pct"] == 95
    # STP-02b (v1.67): alongside the worst-case disclosure, same dialog. Zero
    # markup -> effective % is exactly stop_loss_pct (95.0), the proxy-credit
    # (target_premium) cancelling out of trigger/credit.
    assert D(preview["effective_stop_pct_estimate"]) == D("95.0")

    world["result"] = asyncio.run(manual.fire(
        press_id=world["press_id"], entry_number=1, row=_row(), confirmed=True))


@then('exactly one entry attempt runs through the identical pipeline')
def _(world):
    comp = world["comp"]
    assert world["result"]["result"] == "filled"
    assert len([e for e in comp.events if isinstance(e, EntryWindowOpened)]) == 1
    assert len([e for e in comp.events if isinstance(e, CondorFilled)]) == 1
    assert len(_submitted(comp)) == 1
    assert comp.protected == ["2026-07-06#1"]      # the STP-01 hand-off ran too


@then('the entry is recorded with initiator "manual_entry"')
def _(world):
    filled = [e for e in world["comp"].events if isinstance(e, CondorFilled)]
    assert filled[0].initiator == MANUAL
    assert world["result"]["initiator"] == MANUAL


@then('it counts toward max_entries_per_day')
def _(world):
    """With the cap already reached by this fill, the next manual fire is skipped."""
    comp = world["comp"]
    capped = _manual(comp, max_entries=1)
    out = asyncio.run(capped.fire(press_id="press-2", entry_number=2,
                                  row=_row(), confirmed=True))
    assert out == {"result": "skipped", "reason": "max_entries"}
    assert len(_submitted(comp)) == 1              # no second order


# --- Scenario 2: no fire without the OK dialog ------------------------------------

@given('the operator presses the manual fire button')
def _(world):
    world["comp"] = _Comp()
    world["manual"] = _manual(world["comp"])


@when('the dialog is dismissed or times out')
def _(world):
    world["result"] = asyncio.run(world["manual"].fire(
        press_id="press-1", entry_number=1, row=_row(), confirmed=False))


@then('no order is submitted and no attempt is recorded')
def _(world):
    comp = world["comp"]
    assert world["result"] == {"result": "not_confirmed"}
    assert _submitted(comp) == []
    assert list(comp.events) == []                 # the log is untouched

    # the press was NOT consumed: a dismissed dialog must not disable the button
    ok = asyncio.run(world["manual"].fire(press_id="press-2", entry_number=1,
                                          row=_row(), confirmed=True))
    assert ok["result"] == "filled"


# --- Scenario 3: gates are never bypassed -----------------------------------------

@given('Stop Trading is ON')
def _(world):
    world["comp"] = _Comp(stop_trading=True)
    world["manual"] = _manual(world["comp"])


@when('the operator presses the manual fire button and acknowledges OK')
def _(world):
    assert world["manual"].can_fire() is False     # UI-22: ▶ is disabled
    world["result"] = asyncio.run(world["manual"].fire(
        press_id="press-1", entry_number=1, row=_row(), confirmed=True))


@then('the attempt is refused with skip reason "blocked" shown on the card')
def _(world):
    comp = world["comp"]
    assert world["result"]["result"] == "blocked"
    assert world["result"]["reason"] == "blocked"
    skips = [e for e in comp.events if isinstance(e, EntrySkipped)]
    assert [s.reason for s in skips] == ["blocked"]      # what the card renders
    assert _submitted(comp) == []                        # nothing reached the broker
    assert not [e for e in comp.events if isinstance(e, EntryWindowOpened)]


# --- Scenario 4: RSK-04 vetoes a manual entry like any other ----------------------

@given('open entries whose summed worst case leaves less headroom than the manual entry needs')
def _(world):
    world["comp"] = _Comp()
    # the condor's REAL worst case is (50 - 4) x 100 = 4600; 6000 + 4600 > 10000
    world["manual"] = _manual(world["comp"], risk=RiskSnapshot(
        new_worst_case=D("0"), open_worst_cases=(D("6000"),), max_day_risk=D("10000")))


@when('the manual entry is confirmed')
def _(world):
    world["result"] = asyncio.run(world["manual"].fire(
        press_id="press-1", entry_number=1, row=_row(), confirmed=True))


@then('it is skipped with reason "max_day_risk"')
def _(world):
    comp = world["comp"]
    assert world["result"] == {"result": "skipped", "reason": "max_day_risk"}
    assert [e.reason for e in comp.events if isinstance(e, EntrySkipped)] == ["max_day_risk"]
    assert _submitted(comp) == []                  # RSK-04 vetoed before any order
    assert comp.protected == []


# --- Scenario 5: double-click is one attempt --------------------------------------

@when('the operator presses the button twice and confirms once')
def _(world):
    comp = _Comp()
    manual = _manual(comp)
    world["comp"], world["manual"] = comp, manual

    async def double_click():
        # the SAME press, confirmed twice (a double-click on OK)
        return await asyncio.gather(
            manual.fire(press_id="press-1", entry_number=1, row=_row(), confirmed=True),
            manual.fire(press_id="press-1", entry_number=1, row=_row(), confirmed=True),
        )

    world["results"] = asyncio.run(double_click())


@then('exactly one order exists (idempotency key per press-confirmation)')
def _(world):
    comp = world["comp"]
    assert sorted(r["result"] for r in world["results"]) == ["duplicate_press", "filled"]

    orders = _submitted(comp)
    assert len(orders) == 1
    assert orders[0].idempotency_key == "entry:2026-07-06#1"    # ORD-04: one key
    assert len([e for e in comp.events if isinstance(e, CondorFilled)]) == 1
