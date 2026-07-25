"""TC-CLS-07 (spec v1.87, CLS-03(a)/(b)): a WORKING (pre-fill) entry's clean
cancel is journaled as a terminal `EntryClosed(initiator="cancelled")` and
leaves the open set for good (a); a cancel that discovers a genuine fill
(EC-ENT-06) or races a ladder-minted supersession (b) must never be reported
as a clean "cancelled" (b)(i)/(ii); and "cancelled" alone is never treated as
proof of flatness anywhere, including Flatten All (b)(iii)."""
import asyncio
from decimal import Decimal as D

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.close_entry import CloseEntry
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.manual_close import FLATTEN_CONFIRMATION, ManualClose
from meic.application.persistent_state import PersistentState
from meic.application.working_entries import WorkingEntryOrders
from meic.composition.panel_commands import PanelCommands
from meic.domain.events import CondorFilled, CondorProposed, EntryClosed, ReconciliationMismatch
from meic.domain.projection import fold
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock

from datetime import date, datetime

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))

ENTRY_ID = "2026-07-25#1"


class _NoOpAlerts:
    def alert(self, level: str, message: str, **context) -> None:
        pass


class _CancelBroker:
    """Records every cancel target; `fills` is a controllable list checked
    by the fill-race guard inside `cancel_working`. `cancel_result` (Fix 2,
    v1.87) is returned from every `cancel()` call -- a plain value or a
    zero-arg callable for a per-call sequence -- so a test can drive the
    CONFIRMED/unconfirmed cancel gate; default `None` matches a fake/legacy
    broker that returns nothing (pre-v1.87 behaviour, still confirmed)."""

    def __init__(self, fills=None, cancel_result=None):
        self.cancels: list[str] = []
        self._fills = list(fills or [])
        self._cancel_result = cancel_result

    async def cancel(self, order_id):
        self.cancels.append(str(order_id))
        return self._cancel_result() if callable(self._cancel_result) else self._cancel_result

    async def fills_since(self, cursor):
        return list(self._fills)


def _state():
    return PersistentState(InMemoryStateStore())


class _Comp:
    """Minimal composition-shaped stand-in — just enough surface for
    PanelCommands (events/broker/state/alerts/clock/close/working_entries)."""

    def __init__(self, broker, events, clock):
        self.broker = broker
        self.events = events
        self.state = _state()
        self.alerts = _NoOpAlerts()
        self.clock = clock
        self.close = CloseEntry(broker, events, alerts=self.alerts, clock=clock)
        self.working_entries = WorkingEntryOrders()


def _comp(broker=None, fills=None):
    events: list = []
    broker = broker or _CancelBroker(fills=fills)
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    return _Comp(broker, events, clock)


def test_tc_cls_07_cancelled_pre_fill_entry_leaves_the_open_set():
    """Scenario 1: a WORKING entry with nothing filled, confirmed cancel ->
    a terminal EntryClosed(initiator="cancelled") is journaled, the
    projection no longer shows it open, and a subsequent Flatten All neither
    targets it nor warns about it again."""
    comp = _comp()
    comp.events.append(CondorProposed(entry_id=ENTRY_ID, put_short=D("5000"), call_short=D("5100")))
    comp.working_entries.record(ENTRY_ID, "entry-ord-1")
    cmd = PanelCommands(comp)

    result = asyncio.run(cmd.close(ENTRY_ID))

    assert result == {"result": "cancelled", "initiator": "cancel_entry", "entry_id": ENTRY_ID}
    closed = [e for e in comp.events if isinstance(e, EntryClosed)]
    assert closed == [EntryClosed(entry_id=ENTRY_ID, initiator="cancelled",
                                   at=closed[0].at)]
    assert closed[0].initiator == "cancelled"

    day = fold(comp.events)
    entry = day.entries[ENTRY_ID]
    assert entry.close_initiator == "cancelled"

    # A subsequent Flatten All must not target it (closed) nor warn (incomplete).
    flat = asyncio.run(cmd.flatten(FLATTEN_CONFIRMATION))
    assert ENTRY_ID not in flat["entries"]
    assert all(i["entry_id"] != ENTRY_ID for i in flat["incomplete"])


def test_tc_cls_07_partial_fill_is_never_journalled_as_plain_cancel():
    """Scenario 2: the entry genuinely filled (CondorFilled already
    journaled, EC-ENT-06 territory) by the time the cancel lands -- no
    cancelled-terminal event may be journaled, and the result must never
    read "cancelled"."""
    comp = _comp()
    comp.events.append(CondorFilled(entry_id="e-filled", net_credit=D("3.60")))
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock)

    result = asyncio.run(svc.cancel_working("e-filled", order_id="entry-ord-2"))

    assert result.result != "cancelled"
    assert not any(isinstance(e, EntryClosed) and e.initiator == "cancelled" for e in comp.events)


def test_tc_cls_07_superseded_cancel_is_never_reported_as_clean():
    """Scenario 3: the ladder mints a new working order id mid-replace while
    the panel holds an older snapshot -- the cancel targets the superseded
    id, must re-resolve and retry within its bound (broker.cancel is called
    with each successively newer id), never return "cancelled", and never
    journal a terminal EntryClosed."""
    comp = _comp()
    new_ids = iter(["entry-ord-B", "entry-ord-C", "entry-ord-D"])
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock, cancel_attempts=3)

    result = asyncio.run(svc.cancel_working(
        "e-superseded", order_id="entry-ord-A", current_id=lambda: next(new_ids)))

    assert result.result == "cancel_superseded"
    assert result.result != "cancelled"
    # Each retry targeted the newer id the ladder minted (bounded at 3 attempts).
    assert comp.broker.cancels == ["entry-ord-A", "entry-ord-B", "entry-ord-C"]
    assert not any(isinstance(e, EntryClosed) for e in comp.events)


def test_tc_cls_07_late_fill_after_cancel_reopens_the_entry():
    """Fix 1 (BLOCKING, Opus DO-NOT-SHIP finding): `cancel_working`'s fills-
    feed check can miss a fill that has not yet propagated (the ladder's own
    REPRICE-RACE SWEEP comment documents this). If the ladder later journals
    a CondorFilled for the SAME entry, the earlier "cancelled" terminal is
    STALE -- without this fix, the entry is stuck with
    close_initiator="cancelled" forever, invisible to the stop-fill watcher,
    LEX long recovery, the EOD force-close, Flatten All and the Close
    button, even though it genuinely holds a live position. The projection
    must clear the stale terminal so the entry is open/visible again."""
    events: list = [
        EntryClosed(entry_id=ENTRY_ID, initiator="cancelled"),
        CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.60"),
                     legs=(), put_floor=None, call_floor=None),
    ]

    entry = fold(events).entries[ENTRY_ID]

    assert entry.close_initiator is None
    assert entry.net_credit == D("3.60")


def test_tc_cls_07_genuine_close_survives_a_later_fill():
    """Negative pin for Fix 1: only a STALE "cancelled" terminal is clearable
    by a later fill. A genuine CLS close (e.g. "manual") is never reopened —
    a fill arriving after a real close would be a broker anomaly of an
    entirely different kind, not this rule's business."""
    events: list = [
        EntryClosed(entry_id=ENTRY_ID, initiator="manual"),
        CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.60")),
    ]

    entry = fold(events).entries[ENTRY_ID]

    assert entry.close_initiator == "manual"


class _FillLegsBroker:
    async def fill_legs(self, order_id):
        return ()


def _condor():
    return Condor(entry_number=1, put_short=D("5000"), call_short=D("5100"),
                  put_short_mid=D("2.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("3.00"))


class _RecordingAlerts:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def alert(self, level, message, **context):
        self.calls.append((level, message, context))


def test_tc_cls_07_late_fill_after_cancel_journals_reconciliation_mismatch():
    """Fix 1(b): a fill recorded (`_record_fill`) after a "cancelled"
    terminal was already journaled for the SAME entry must NOT self-heal
    silently -- it appends a `ReconciliationMismatch` naming the entry, and
    fires a critical alert through the wired alerts sink."""
    events: list = [EntryClosed(entry_id=ENTRY_ID, initiator="cancelled")]
    alerts = _RecordingAlerts()
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=alerts)

    asyncio.run(attempt._record_fill(
        ENTRY_ID, "working-1", _condor(), date(2026, 7, 25), D("3.60"), "schedule"))

    mismatches = [e for e in events if isinstance(e, ReconciliationMismatch)]
    assert len(mismatches) == 1
    assert ENTRY_ID in mismatches[0].detail
    assert any(level == "critical" for level, _msg, _ctx in alerts.calls)


def test_tc_cls_07_fill_without_a_prior_cancel_journals_no_mismatch():
    """Negative pin: an ordinary fill with no prior "cancelled" terminal for
    this entry must NOT trip Fix 1(b)'s anomaly detector."""
    events: list = []
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=_RecordingAlerts())

    asyncio.run(attempt._record_fill(
        ENTRY_ID, "working-1", _condor(), date(2026, 7, 25), D("3.60"), "schedule"))

    assert not any(isinstance(e, ReconciliationMismatch) for e in events)


def test_tc_cls_07_unconfirmed_cancel_is_superseded_not_terminal():
    """Fix 2: CLS-03(a) authorises the terminal journal only on a CONFIRMED
    cancel. A broker returning {"result": "error"} (TastytradeAdapter's
    shape for ANY cancel failure) must NOT be trusted -- "cancel_superseded",
    never "cancelled", and no terminal EntryClosed lands in the journal."""
    comp = _comp(broker=_CancelBroker(cancel_result={"result": "error"}))
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock)

    result = asyncio.run(svc.cancel_working("e-unconfirmed", order_id="entry-ord-5"))

    assert result.result == "cancel_superseded"
    assert not any(isinstance(e, EntryClosed) for e in comp.events)


def test_tc_cls_07_confirmed_cancel_shapes_still_journal_cancelled():
    """Fix 2 counterpart: an explicit {"result": "cancelled"} and a bare
    `None` (fakes/legacy brokers that return nothing) both still count as
    CONFIRMED -- the pre-v1.87 behaviour for well-behaved brokers/fakes is
    unchanged."""
    for cancel_result in ({"result": "cancelled"}, None):
        comp = _comp(broker=_CancelBroker(cancel_result=cancel_result))
        svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                           events=comp.events, clock=comp.clock)

        result = asyncio.run(svc.cancel_working("e-confirmed", order_id="entry-ord-6"))

        assert result.result == "cancelled", f"{cancel_result!r} should confirm the cancel"
        assert any(isinstance(e, EntryClosed) and e.initiator == "cancelled" for e in comp.events)


def test_tc_cls_07_concurrent_double_click_yields_exactly_one_cancel():
    """Fix 3 correction: two rapid operator clicks are two concurrent FastAPI
    tasks on the same event loop. `_done` must latch EAGERLY (before the
    first `await self.broker.cancel(...)`), not only after it -- otherwise
    the second click enters while `_done` is still empty, passes the guard,
    and issues a SECOND broker cancel for the same entry, breaking "a
    double-click yields exactly one cancel" and handing the race's loser a
    scary cancel_superseded toast for a cancel that actually succeeded."""
    entered = asyncio.Event()
    proceed = asyncio.Event()

    class _SuspendingBroker(_CancelBroker):
        async def cancel(self, order_id):
            # Suspend INSIDE cancel() so a concurrent second call gets a
            # chance to run while the first is still awaiting -- exactly
            # the two-tasks-same-event-loop race the fix closes.
            entered.set()
            await proceed.wait()
            return await super().cancel(order_id)

    comp = _comp(broker=_SuspendingBroker())
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock)

    async def _drive():
        first_task = asyncio.ensure_future(svc.cancel_working("e-concurrent", order_id="entry-ord-11"))
        await entered.wait()          # the first call is now suspended inside cancel()
        second = await svc.cancel_working("e-concurrent", order_id="entry-ord-11")
        proceed.set()                 # release the first call to complete
        first = await first_task
        return first, second

    first, second = asyncio.run(_drive())

    results = {first.result, second.result}
    assert results == {"cancelled", "already_done"}
    assert comp.broker.cancels == ["entry-ord-11"]  # the broker was contacted exactly once


def test_tc_cls_07_exception_from_broker_cancel_releases_the_latch():
    """Fix 3 correction: an exception escaping `broker.cancel()` must not
    strand the operator behind `already_done` -- the eager latch is
    released in `finally` (terminal stays False), so a subsequent call
    genuinely reaches the broker again."""
    calls = {"n": 0}

    class _RaisingOnceBroker(_CancelBroker):
        async def cancel(self, order_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("broker unavailable")
            return await super().cancel(order_id)

    comp = _comp(broker=_RaisingOnceBroker())
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock)

    try:
        asyncio.run(svc.cancel_working("e-raises", order_id="entry-ord-12"))
        assert False, "expected the broker's exception to propagate"
    except RuntimeError:
        pass

    second = asyncio.run(svc.cancel_working("e-raises", order_id="entry-ord-12"))

    assert second.result == "cancelled"
    assert comp.broker.cancels == ["entry-ord-12"]  # the raising call never recorded a target


def test_tc_cls_07_second_press_after_superseded_actually_retries_the_broker():
    """Fix 3: `_done` must latch ONLY on a genuinely terminal outcome.
    After "cancel_superseded" (an UNCONFIRMED cancel), the operator has no
    recourse if a second press is silently swallowed as `already_done` — it
    must reach the broker again."""
    comp = _comp(broker=_CancelBroker(cancel_result={"result": "error"}))
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock)

    first = asyncio.run(svc.cancel_working("e-retry", order_id="entry-ord-7"))
    second = asyncio.run(svc.cancel_working("e-retry", order_id="entry-ord-7"))

    assert first.result == "cancel_superseded"
    assert second.result == "cancel_superseded"
    # The broker was genuinely re-contacted on the second press, not skipped.
    assert comp.broker.cancels == ["entry-ord-7", "entry-ord-7"]


def test_tc_cls_07_second_press_after_clean_cancel_is_a_true_no_op():
    """Fix 3 counterpart: a genuinely CLEAN "cancelled" result still latches
    -- a double-click on an already-cleanly-cancelled entry must not
    double-cancel (idempotency, ORD-04)."""
    comp = _comp()
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock)

    first = asyncio.run(svc.cancel_working("e-once", order_id="entry-ord-8"))
    second = asyncio.run(svc.cancel_working("e-once", order_id="entry-ord-8"))

    assert first.result == "cancelled"
    assert second.result == "already_done"
    assert comp.broker.cancels == ["entry-ord-8"]  # broker contacted exactly once


def test_tc_cls_07_condorfilled_for_a_different_entry_does_not_block_this_cancel():
    """Strengthens scenario 2 (Opus finding 8): the CLS-03(a) CondorFilled
    guard must match on `entry_id`, not merely "a CondorFilled exists
    anywhere in the journal" -- a fill for a DIFFERENT entry must not
    suppress THIS entry's genuinely clean cancel."""
    comp = _comp()
    comp.events.append(CondorFilled(entry_id="some-other-entry", net_credit=D("3.60")))
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock)

    result = asyncio.run(svc.cancel_working("e-unrelated", order_id="entry-ord-10"))

    assert result.result == "cancelled"
    assert any(isinstance(e, EntryClosed) and e.entry_id == "e-unrelated"
               and e.initiator == "cancelled" for e in comp.events)


def test_tc_cls_07_cancelled_is_never_treated_as_flat():
    """Scenario 4: a clean "cancelled" result must land in Flatten All's
    `incomplete` list, never in `entries` -- an all-clear requires PROVEN
    flatness from broker truth, not a cancel result alone."""
    comp = _comp()
    comp.events.append(CondorProposed(entry_id=ENTRY_ID, put_short=D("5000"), call_short=D("5100")))
    comp.working_entries.record(ENTRY_ID, "entry-ord-9")
    cmd = PanelCommands(comp)

    result = asyncio.run(cmd.flatten(FLATTEN_CONFIRMATION))

    assert result["result"] == "flattened"
    assert ENTRY_ID not in result["entries"]
    assert len(result["incomplete"]) == 1
    assert result["incomplete"][0]["entry_id"] == ENTRY_ID
    assert result["incomplete"][0]["result"] == "cancelled"
