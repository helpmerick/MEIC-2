"""TC-CLS-02 (UC-14/UI-16): Close fires instantly with no confirmation dialog,
closes via CLS, clears the armed TPF floor, tags the report `manual`, is
idempotent under a double-click; a WORKING entry is cancelled (CLS-03); flatten
still requires a typed FLATTEN confirmation (TC-FLT-01)."""
import asyncio
from decimal import Decimal as D

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.manual_close import ManualClose
from meic.application.persistent_state import PersistentState
from meic.domain.events import EntryClosed
from meic.domain.projection import day_report


class RecordingBroker:
    def __init__(self):
        self.submits = 0
        self.cancels = []

    async def submit(self, order):
        self.submits += 1
        return f"ord-{self.submits}"

    async def cancel(self, id):
        self.cancels.append(id)
        return {"result": "cancelled"}

    async def replace(self, id, new):
        # CLS-01 v1.50: CloseEntry replaces a short's resting stop in ONE port
        # call rather than a bare cancel() + submit(). This bare recording
        # double doesn't track real order state, so it just records the old
        # id (mirroring the old cancel-first log) and submits the new intent.
        self.cancels.append(id)
        return await self.submit(new)


def _svc(events, state):
    broker = RecordingBroker()
    return ManualClose(CloseEntry(broker, events), broker, state), broker


def _state(**floors):
    s = PersistentState(InMemoryStateStore())
    s.tpf_floors = floors
    return s


def test_tc_cls_02_close_is_instant_via_cls_clears_tpf_tags_manual():
    """Close needs no confirmation (instant, no dialog); routes through CLS with
    initiator `manual`; clears the entry's armed TPF floor; report tags manual."""
    events: list = []
    state = _state(**{"e1": "6.00"})  # e1 has an armed TPF floor
    svc, broker = _svc(events, state)

    assert svc.requires_close_confirmation() is False  # Bug #16 — no dialog

    res = asyncio.run(svc.close(
        "e1", live_legs=[LiveLeg("P", "PUT", "short", -1), LiveLeg("C", "CALL", "short", -1)],
        resting_stop_ids={"PUT": "sP", "CALL": "sC"}, close_price=D("0.05")))

    assert res.result == "closed" and res.initiator == "manual"
    assert set(broker.cancels) == {"sP", "sC"}                 # stops replaced (CLS-01 v1.50)
    closed = [e for e in events if isinstance(e, EntryClosed)]
    assert closed == [EntryClosed(entry_id="e1", initiator="manual")]
    assert state.tpf_floors == {}                              # armed TPF floor cleared
    # the report tags the close `manual` (per-entry attribution survives)
    assert "e1" in day_report(events).per_entry_pnl


def test_tc_cls_02_double_click_produces_exactly_one_close():
    """A rapid double-click is idempotent — exactly one close, no duplicate
    orders (CLS-03 idempotency)."""
    events: list = []
    state = _state()
    svc, broker = _svc(events, state)
    legs = [LiveLeg("P", "PUT", "short", -1)]

    first = asyncio.run(svc.close("e1", live_legs=legs, resting_stop_ids={}, close_price=D("0.05")))
    second = asyncio.run(svc.close("e1", live_legs=legs, resting_stop_ids={}, close_price=D("0.05")))

    assert first.result == "closed" and second.result == "already_done"
    assert sum(isinstance(e, EntryClosed) for e in events) == 1   # exactly one close
    assert broker.submits == 1                                    # no duplicate leg orders


def test_tc_cls_02_working_entry_is_cancelled_not_closed():
    """On a WORKING (pre-fill) entry the action is Cancel entry (CLS-03), also
    instant — no close orders placed for its unfilled legs."""
    events: list = []
    state = _state(**{"e2": "5.00"})
    svc, broker = _svc(events, state)

    res = asyncio.run(svc.cancel_working("e2", order_id="entry-ord-2"))

    assert res.result == "cancelled" and res.initiator == "cancel_entry"
    assert broker.cancels == ["entry-ord-2"] and broker.submits == 0  # no close orders
    assert not any(isinstance(e, EntryClosed) for e in events)
    assert state.tpf_floors == {}


def test_tc_cls_02_flatten_all_requires_typed_confirmation():
    """Flatten-all is the one control that still requires a typed FLATTEN
    confirmation (contrast: Close is instant)."""
    assert ManualClose.may_flatten("FLATTEN") is True
    assert ManualClose.may_flatten("flatten") is False
    assert ManualClose.may_flatten("") is False
