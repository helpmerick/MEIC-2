"""TC-CAL-02 -- CAL-06 manual override (doc 11, v1.71).

Binds the BACKEND half: a manual (Y) fire on a NO-TRADE-tagged ET day never
hard-blocks (ENT-09's fresh-intent rationale) but requires the explicit
`blackout_ack` flag -- refused with a distinct, label-carrying reason
without it (the backend equivalent of "OK stays disabled until
acknowledged" -- the dialog itself is slice 2's frontend), evented and
report-tagged `blackout_overridden` with it.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then

from meic.application.calendar_store import CalendarStore
from meic.application.entry_gates import GateSnapshot
from meic.application.event_log import EventLog
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.manual_entry import ManualEntry
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.domain.events import CondorFilled, EntrySkipped, ManualFireBlackoutAcknowledged
from meic.domain.projection import fold
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker

scenarios("../features/TC-CAL-02.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
NOW = datetime(2026, 7, 15, 10, 7, tzinfo=timezone.utc)
DAY = "2026-07-15"
IS_CONDOR = lambda o: o.kind == "iron_condor"


def _row(**over):
    from meic.domain.schedule import ResolvedEntry
    from datetime import time as dtime

    base = dict(time=dtime(10, 0), contracts=1, target_premium=D("3.00"), wing_width=D("50"),
                stop_loss_pct=95, stop_basis="total_credit", stop_rebate_markup=D("0.00"),
                min_short_premium=D("1.00"), min_total_credit=D("2.00"), probe_down_max=25,
                strike_method="premium", short_delta_target=D("0.10"))
    return ResolvedEntry(**{**base, **over})


def _condor(n=1, contracts=1):
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5940"), call_long=D("6110"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 7, 15), contracts=contracts)


class _Clock:
    def now(self):
        return NOW

    async def wait_until(self, when):
        return None


class _Comp:
    def __init__(self) -> None:
        self.broker = FakeBroker()
        self.broker.autofill(IS_CONDOR)
        self.events = EventLog(config_version="v1.71")
        self.clock = _Clock()
        self.execute = ExecuteEntryAttempt(self.broker, self.clock, self.events, SPX)
        self.state = PersistentState(InMemoryStateStore())
        self.state.armed = True
        self.state.confirm_live = True
        self.state.stop_trading = False
        self.protected: list = []

    async def _on_filled(self, entry_id, condor, stop=None, fill_credit=None):
        self.protected.append(entry_id)


async def _gates():
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


@pytest.fixture
def world():
    return {}


@given('today is tagged NO-TRADE "FOMC" and the operator presses the manual fire button')
def _(world):
    comp = _Comp()
    store = CalendarStore(comp.events, comp.clock)
    store.tag(DAY, "FOMC")

    async def selector(when, n, config=None, put_floor=None, call_floor=None):
        return _condor(n, config.contracts if config else 1), None

    manual = ManualEntry(comp, selector, _gates, day=lambda: DAY,
                         calendar_label=store.label_for_day)
    world.update(comp=comp, store=store, manual=manual)

    # The unacknowledged press first -- the OK dialog's disabled state,
    # backend half: firing without the checkbox is REFUSED, never silently
    # dropped and never a hard block (ENT-09's fresh-intent rationale).
    world["unacked_result"] = asyncio.run(
        manual.fire(press_id="press-1", entry_number=1, row=_row(), confirmed=True))


@then("the OK dialog shows the blackout warning and OK stays disabled until acknowledged")
def _(world):
    comp = world["comp"]
    result = world["unacked_result"]
    assert result["reason"] == "blackout_unacknowledged:FOMC"
    assert result["blackout_label"] == "FOMC"
    # never silent: a distinct, label-carrying EntrySkipped landed, and no
    # order reached the broker.
    skips = [e for e in comp.events if isinstance(e, EntrySkipped)]
    assert [s.reason for s in skips] == ["blackout_unacknowledged:FOMC"]
    assert [o.intent for o in comp.broker._orders.values() if o.intent.kind == "iron_condor"] == []
    assert not any(isinstance(e, ManualFireBlackoutAcknowledged) for e in comp.events)


@then('an acknowledged fire proceeds, is evented, and reports tagged "blackout_overridden"')
def _(world):
    comp, manual = world["comp"], world["manual"]
    # Final-review finding 3 (2026-07-15), pinned: the refused unacknowledged
    # press above must NOT have consumed its press_id -- the acknowledged
    # retry arrives with the SAME id (the dialog holds it) and must fire.
    result = asyncio.run(manual.fire(
        press_id="press-1", entry_number=1, row=_row(), confirmed=True, blackout_ack=True))

    assert result["result"] == "filled"
    assert result["blackout_overridden"] is True

    acks = [e for e in comp.events if isinstance(e, ManualFireBlackoutAcknowledged)]
    assert len(acks) == 1 and acks[0].day == DAY and acks[0].label == "FOMC"

    filled = [e for e in comp.events if isinstance(e, CondorFilled)]
    assert len(filled) == 1 and filled[0].blackout_overridden is True

    day = fold(comp.events)
    entry = day.entries[result["entry_id"]]
    assert entry.blackout_overridden is True   # "the entry is report-tagged blackout_overridden"

    # ... and exactly once: the press IS consumed by the fire that proceeded,
    # so a double-confirm of the same press stays one attempt (UI-22) and the
    # acknowledgment is never journaled twice.
    dup = asyncio.run(manual.fire(
        press_id="press-1", entry_number=1, row=_row(), confirmed=True, blackout_ack=True))
    assert dup == {"result": "duplicate_press", "press_id": "press-1"}
    assert len([e for e in comp.events if isinstance(e, CondorFilled)]) == 1
    assert len([e for e in comp.events if isinstance(e, ManualFireBlackoutAcknowledged)]) == 1
