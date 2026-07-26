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
from meic.application.execute_entry import Condor, ExecuteEntryAttempt, _TERMINAL_SKIP_INITIATOR
from meic.application.manual_close import FLATTEN_CONFIRMATION, ManualClose
from meic.application.persistent_state import PersistentState
from meic.application.working_entries import WorkingEntryOrders
from meic.composition.panel_commands import PanelCommands
from meic.domain.events import CondorFilled, CondorProposed, EntryClosed, ReconciliationMismatch
from meic.domain.projection import STALE_TERMINAL_INITIATORS, fold
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

    async def fills_since(self):
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


def test_tc_cls_07_late_fill_after_unfilled_terminal_reopens_the_entry():
    """CLS-03(a2) v1.88 extension of Fix 1: the ladder's own "unfilled"
    terminal (`unfilled_at_floor` skip, _TERMINAL_SKIP_INITIATOR) is just as
    stale-able as ManualClose's "cancelled" -- a late-propagating fill after
    it means the entry genuinely holds a position and must reopen exactly
    like the "cancelled" case above."""
    events: list = [
        EntryClosed(entry_id=ENTRY_ID, initiator="unfilled"),
        CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.60"),
                     legs=(), put_floor=None, call_floor=None),
    ]

    entry = fold(events).entries[ENTRY_ID]

    assert entry.close_initiator is None
    assert entry.net_credit == D("3.60")


def test_tc_cls_07_late_fill_after_cancelled_by_operator_terminal_reopens_the_entry():
    """CLS-03(a2) v1.88 extension of Fix 1: same staleness story for the
    ladder's "cancelled_by_operator" terminal (the operator cancelled a
    WORKING entry from the panel and the ladder's own stand-down path
    journaled it, as opposed to ManualClose's "cancelled")."""
    events: list = [
        EntryClosed(entry_id=ENTRY_ID, initiator="cancelled_by_operator"),
        CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.60"),
                     legs=(), put_floor=None, call_floor=None),
    ]

    entry = fold(events).entries[ENTRY_ID]

    assert entry.close_initiator is None
    assert entry.net_credit == D("3.60")


def test_tc_cls_07_cancel_working_never_journals_a_second_terminal():
    """Never-two-terminals guard (mirrors execute_entry.py's `_skip`): a
    prior EntryClosed already sits in the log for this entry_id (e.g. the
    ladder's own v1.88 "unfilled" terminal landed first). `cancel_working`
    must report the honest outcome "already_terminal" (Fix 2, v1.88: this
    press did not cancel anything -- the entry was already terminal, and
    flatness is NOT proven from broker truth on this path, so it must never
    read as flat -- "already_closed" would wrongly land in Flatten's flat
    set), NOT "cancelled", and must NOT append a second EntryClosed for the
    same entry_id."""
    comp = _comp()
    entry_id = "e-already-terminal"
    comp.events.append(EntryClosed(entry_id=entry_id, initiator="unfilled"))
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock)

    before = sum(1 for e in comp.events if isinstance(e, EntryClosed) and e.entry_id == entry_id)
    assert before == 1

    result = asyncio.run(svc.cancel_working(entry_id, order_id="entry-ord-already-terminal"))

    after = sum(1 for e in comp.events if isinstance(e, EntryClosed) and e.entry_id == entry_id)
    assert after == 1
    assert result.result == "already_terminal"
    assert result.result != "cancelled"
    assert result.initiator == "cancel_entry"


def test_tc_cls_07_cancel_working_never_claims_cancelled_for_a_different_prior_terminal():
    """Extension: the prior terminal's initiator is truthfully "unfilled"
    (the market never paid) -- a later cancel press must NOT relabel that
    as "cancelled" (the convenient-label failure v1.88 exists to prevent).
    It must report "already_terminal" (Fix 2) and leave exactly ONE
    EntryClosed for this entry_id, both before and after the call."""
    comp = _comp()
    entry_id = "e-already-terminal-unfilled"
    comp.events.append(EntryClosed(entry_id=entry_id, initiator="unfilled"))
    svc = ManualClose(comp.close, comp.broker, comp.state, alerts=comp.alerts,
                       events=comp.events, clock=comp.clock)

    def _count():
        return sum(1 for e in comp.events if isinstance(e, EntryClosed) and e.entry_id == entry_id)

    assert _count() == 1

    result = asyncio.run(svc.cancel_working(entry_id, order_id="entry-ord-already-terminal-unfilled"))

    assert result.result == "already_terminal"
    assert result.result != "cancelled"
    assert _count() == 1


def test_tc_cls_07_already_terminal_is_never_treated_as_flat():
    """Fix 2 (2026-07-25 Opus DO-NOT-SHIP finding): a flatten operation whose
    per-entry close returns "already_terminal" must land that entry in
    `incomplete`, never in `entries` -- mirrors
    `test_tc_cls_07_cancelled_is_never_treated_as_flat` above. Unlike
    "already_closed" (which IS in panel_commands.py's flat tuple, and only
    ever surfaces when the projection already shows the entry closed --
    `flatten`'s own open-entries scan would never call `close()` for it at
    all), "already_terminal" is reached the SAME way `cancel_working` itself
    reaches it: a terminal races in DURING `close()`'s own broker round trip,
    after `flatten`'s open-entries scan already decided to act on this
    entry_id. `PanelCommands.close` is monkeypatched here to return that
    outcome directly -- exercising `flatten`'s own classification of the
    result string, independent of driving the underlying race a second time
    (already covered by
    test_tc_cls_07_cancel_working_races_ladder_skip_yields_exactly_one_terminal)."""
    comp = _comp()
    entry_id = "e-already-terminal-flatten"
    comp.events.append(CondorProposed(entry_id=entry_id, put_short=D("5000"), call_short=D("5100")))
    comp.working_entries.record(entry_id, "entry-ord-already-terminal-flatten")
    cmd = PanelCommands(comp)

    async def _fake_close(eid):
        return {"result": "already_terminal", "initiator": "cancel_entry", "entry_id": eid}

    cmd.close = _fake_close  # type: ignore[method-assign]

    flat = asyncio.run(cmd.flatten(FLATTEN_CONFIRMATION))

    assert entry_id not in flat["entries"]
    assert any(i["entry_id"] == entry_id and i["result"] == "already_terminal"
               for i in flat["incomplete"])


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


def test_tc_cls_07_late_fill_after_unfilled_terminal_journals_reconciliation_mismatch():
    """v1.88 extension of Fix 1(b): a fill landing (`_record_fill`) after a
    v1.88 "unfilled" terminal was already journaled for the SAME entry must
    not self-heal silently either -- same ReconciliationMismatch + critical
    alert as the pre-existing "cancelled" case."""
    events: list = [EntryClosed(entry_id=ENTRY_ID, initiator="unfilled")]
    alerts = _RecordingAlerts()
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=alerts)

    asyncio.run(attempt._record_fill(
        ENTRY_ID, "working-1", _condor(), date(2026, 7, 25), D("3.60"), "schedule"))

    mismatches = [e for e in events if isinstance(e, ReconciliationMismatch)]
    assert len(mismatches) == 1
    assert ENTRY_ID in mismatches[0].detail
    assert any(level == "critical" for level, _msg, _ctx in alerts.calls)


def test_tc_cls_07_late_fill_after_cancelled_by_operator_terminal_journals_reconciliation_mismatch():
    """v1.88 extension of Fix 1(b): a fill landing (`_record_fill`) after a
    v1.88 "cancelled_by_operator" terminal was already journaled for the
    SAME entry must not self-heal silently either -- same
    ReconciliationMismatch + critical alert as the pre-existing "cancelled"
    case."""
    events: list = [EntryClosed(entry_id=ENTRY_ID, initiator="cancelled_by_operator")]
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


def test_tc_cls_07_cancel_working_races_ladder_skip_yields_exactly_one_terminal():
    """Fix 3(b): races the panel's real cancel path (`cancel_working`)
    against the entry ladder's real `_skip`-driven v1.88 terminal (via
    `ExecuteEntryAttempt`) for the SAME entry_id, using `asyncio.gather` --
    same suspending-broker technique as
    test_tc_cls_07_concurrent_double_click_yields_exactly_one_cancel.

    CORRECTED (2026-07-25, Opus DO-NOT-SHIP finding 5): this docstring used
    to claim the ordering "is genuinely arbitrated by asyncio.gather, not
    hard-sequenced by this test" -- that was FALSE. `_skip_after_entered`
    only starts its own work after `await entered.wait()`, and `entered` is
    only set from INSIDE `_SuspendingBroker.cancel()` -- i.e. after
    `cancel_working` has already reached its check-then-append point and
    suspended waiting on `proceed`. That makes this a DETERMINISTIC
    interleaving, not a genuine race: `_skip` is forced to complete first,
    every single run, because `cancel_working` cannot resume until
    `proceed.set()` -- which only happens AFTER `_skip` has already run.
    `_skip` itself has no `await` inside its check-then-append (the
    INVARIANT the comments above both blocks in manual_close.py and
    execute_entry.py pin), so once it starts it always completes atomically
    before yielding control back. The test still earns its keep: it proves
    `cancel_working`, having suspended INSIDE `broker.cancel()` before its
    own check-then-append runs, correctly observes the ladder's terminal
    that landed while it was down and reports the honest outcome instead of
    a stale "cancelled" -- see the MIRROR test directly below for the
    opposite ordering (cancel_working's own terminal landing first)."""
    day = "2026-07-25"
    n = 1
    entry_id = f"{day}#{n}"
    events: list = [CondorProposed(entry_id=entry_id, put_short=D("5000"), call_short=D("5100"))]

    entered = asyncio.Event()
    proceed = asyncio.Event()

    class _SuspendingBroker(_CancelBroker):
        async def cancel(self, order_id):
            # Suspend INSIDE cancel() so the ladder's `_skip` genuinely gets
            # a chance to run while `cancel_working` is still awaiting --
            # the real race the INVARIANT comments protect against.
            entered.set()
            await proceed.wait()
            return await super().cancel(order_id)

    broker = _SuspendingBroker()
    state = _state()
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    alerts = _NoOpAlerts()
    svc = ManualClose(CloseEntry(broker, events, alerts=alerts, clock=clock),
                       broker, state, alerts=alerts, events=events, clock=clock)
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=alerts)

    async def _cancel():
        return await svc.cancel_working(entry_id, order_id="ladder-order-1")

    async def _skip_after_entered():
        await entered.wait()  # cancel_working is now suspended inside broker.cancel()
        result = attempt._skip(day, n, "unfilled_at_floor")
        proceed.set()          # release cancel_working to complete
        return result

    async def _drive():
        return await asyncio.gather(_cancel(), _skip_after_entered())

    cancel_result, skip_result = asyncio.run(_drive())

    terminals = [e for e in events if isinstance(e, EntryClosed) and e.entry_id == entry_id]
    assert len(terminals) == 1
    # `_skip` has no await point of its own, so once the loop schedules it
    # (always after `cancel_working` has already suspended, per the wiring
    # above) it always wins and journals the honest "unfilled" terminal --
    # `cancel_working` must then see the entry as already terminal and
    # report that honestly, never claim "cancelled".
    assert terminals[0].initiator == "unfilled"
    assert cancel_result.result == "already_terminal"


def test_tc_cls_07_ladder_skip_races_cancel_working_journalling_first_yields_exactly_one_terminal():
    """Fix 5 (mirror ordering, 2026-07-25 Opus DO-NOT-SHIP finding): the
    test directly above only ever exercises `_skip` completing FIRST (see
    its corrected docstring) -- the opposite ordering, `cancel_working`
    journalling its own "cancelled" terminal FIRST and `_skip` then having
    to observe THAT and append nothing, was previously covered only by
    non-concurrent/static tests, never by this module's own
    asyncio.gather/suspending-broker race structure. This test flips which
    side is allowed to complete first. `_skip` has no I/O of its own to
    suspend a broker inside, so there is nothing to gate it on except plain
    sequencing: `cancel_working` is given a broker that resolves
    IMMEDIATELY (no suspension) and is run to completion -- including its
    own EntryClosed append -- before `_skip` is ever invoked. `_skip` must
    then observe the entry as already terminal and append NOTHING: exactly
    one EntryClosed remains for the entry_id, and its initiator is
    "cancelled" (cancel_working won this ordering)."""
    day = "2026-07-25"
    n = 2
    entry_id = f"{day}#{n}"
    events: list = [CondorProposed(entry_id=entry_id, put_short=D("5000"), call_short=D("5100"))]

    broker = _CancelBroker()  # resolves cancel()/fills_since() immediately, no suspension
    state = _state()
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    alerts = _NoOpAlerts()
    svc = ManualClose(CloseEntry(broker, events, alerts=alerts, clock=clock),
                       broker, state, alerts=alerts, events=events, clock=clock)
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=alerts)

    cancel_result = asyncio.run(svc.cancel_working(entry_id, order_id="ladder-order-2"))
    # cancel_working has now fully completed and journalled its own terminal
    # -- `_skip` runs strictly AFTER, exactly mirroring the "cancel journals
    # first" ordering the finding calls out as previously uncovered here.
    skip_result = attempt._skip(day, n, "unfilled_at_floor")

    terminals = [e for e in events if isinstance(e, EntryClosed) and e.entry_id == entry_id]
    assert len(terminals) == 1
    assert terminals[0].initiator == "cancelled"
    assert cancel_result.result == "cancelled"
    assert skip_result.status == "SKIPPED"


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


# --- CLS-03(a2) v1.88: terminal-journaling skip reasons ---------------------


def test_tc_cls_07_unfilled_at_floor_journals_unfilled_terminal():
    """Scenario 4: the ladder priced out at the floor unfilled -- `_skip`
    journals a terminal EntryClosed(initiator="unfilled"), never "cancelled"
    (nobody cancelled anything; the market didn't pay)."""
    day, n = "2026-07-25", 1
    entry_id = f"{day}#{n}"
    events: list = [CondorProposed(entry_id=entry_id, put_short=D("5000"), call_short=D("5100"))]
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=_RecordingAlerts())

    attempt._skip(day, n, "unfilled_at_floor")

    closed = [e for e in events if isinstance(e, EntryClosed) and e.entry_id == entry_id]
    assert len(closed) == 1
    assert closed[0].initiator == "unfilled"

    entry = fold(events).entries[entry_id]
    assert entry.close_initiator == "unfilled"

    comp = _comp()
    comp.events.extend(events)
    cmd = PanelCommands(comp)
    flat = asyncio.run(cmd.flatten(FLATTEN_CONFIRMATION))
    assert entry_id not in flat["entries"]
    assert all(i["entry_id"] != entry_id for i in flat["incomplete"])


def test_tc_cls_07_operator_cancel_ladder_skip_journals_cancelled_by_operator():
    """Scenario 5: the operator cancelled a WORKING entry from the schedule/
    ladder path -- `_skip` journals a terminal
    EntryClosed(initiator="cancelled_by_operator")."""
    day, n = "2026-07-25", 2
    entry_id = f"{day}#{n}"
    events: list = [CondorProposed(entry_id=entry_id, put_short=D("5000"), call_short=D("5100"))]
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=_RecordingAlerts())

    attempt._skip(day, n, "cancelled_by_operator")

    closed = [e for e in events if isinstance(e, EntryClosed) and e.entry_id == entry_id]
    assert len(closed) == 1
    assert closed[0].initiator == "cancelled_by_operator"

    entry = fold(events).entries[entry_id]
    assert entry.close_initiator == "cancelled_by_operator"

    comp = _comp()
    comp.events.extend(events)
    cmd = PanelCommands(comp)
    flat = asyncio.run(cmd.flatten(FLATTEN_CONFIRMATION))
    assert entry_id not in flat["entries"]
    assert all(i["entry_id"] != entry_id for i in flat["incomplete"])


def test_tc_cls_07_submit_indeterminate_journals_no_terminal():
    """(iii) fail-closed: `submit_indeterminate` is deliberately absent from
    the terminal-skip map -- the submit's outcome is UNKNOWN, so no terminal
    may be journaled."""
    day, n = "2026-07-25", 3
    entry_id = f"{day}#{n}"
    events: list = [CondorProposed(entry_id=entry_id, put_short=D("5000"), call_short=D("5100"))]
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=_RecordingAlerts())

    attempt._skip(day, n, "submit_indeterminate")

    assert not any(isinstance(e, EntryClosed) and e.entry_id == entry_id for e in events)


def test_tc_cls_07_pre_proposal_gate_skip_journals_no_terminal():
    """A pre-proposal gate skip (no CondorProposed journaled for this entry
    yet) must never create a phantom terminal -- covers both a reason absent
    from the terminal-skip map (`missed_window`) and, defensively, one that
    IS in the map but fires before any CondorProposed exists."""
    day, n = "2026-07-25", 4
    entry_id = f"{day}#{n}"
    events: list = []  # no CondorProposed at all
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=_RecordingAlerts())

    attempt._skip(day, n, "missed_window")
    assert not any(isinstance(e, EntryClosed) and e.entry_id == entry_id for e in events)

    attempt._skip(day, n, "unfilled_at_floor")  # in the map, but still no CondorProposed
    assert not any(isinstance(e, EntryClosed) and e.entry_id == entry_id for e in events)


def test_tc_cls_07_broker_truth_wins_over_a_stale_skip():
    """Broker truth wins: a CondorFilled is already journaled for this entry
    when a stale skip arrives -- no new EntryClosed terminal, but a loud
    ReconciliationMismatch (and critical alert) naming the entry."""
    day, n = "2026-07-25", 5
    entry_id = f"{day}#{n}"
    events: list = [
        CondorProposed(entry_id=entry_id, put_short=D("5000"), call_short=D("5100")),
        CondorFilled(entry_id=entry_id, net_credit=D("3.60")),
    ]
    alerts = _RecordingAlerts()
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=alerts)

    attempt._skip(day, n, "unfilled_at_floor")

    assert not any(isinstance(e, EntryClosed) and e.entry_id == entry_id for e in events)
    mismatches = [e for e in events if isinstance(e, ReconciliationMismatch)]
    assert len(mismatches) == 1
    assert entry_id in mismatches[0].detail
    assert any(level == "critical" for level, _msg, _ctx in alerts.calls)


def test_tc_cls_07_terminal_skip_is_idempotent():
    """A second terminal skip for an already-closed entry must never
    duplicate the terminal EntryClosed."""
    day, n = "2026-07-25", 6
    entry_id = f"{day}#{n}"
    events: list = [
        CondorProposed(entry_id=entry_id, put_short=D("5000"), call_short=D("5100")),
        EntryClosed(entry_id=entry_id, initiator="unfilled"),
    ]
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=ET))
    attempt = ExecuteEntryAttempt(_FillLegsBroker(), clock, events, SPX, alerts=_RecordingAlerts())

    attempt._skip(day, n, "unfilled_at_floor")

    closed = [e for e in events if isinstance(e, EntryClosed) and e.entry_id == entry_id]
    assert len(closed) == 1


def test_tc_cls_07_rpt03_cancelled_and_unfilled_outcomes():
    """RPT-03 v1.88: `classify()` maps "cancelled"/"cancelled_by_operator" to
    CANCELLED, "unfilled" to UNFILLED, and an unrecognised initiator still
    falls through to EXTERNAL (negative pin)."""
    from meic.reporting.taxonomy import CANCELLED, EXTERNAL, UNFILLED, classify

    for initiator, expected in (
        ("cancelled", CANCELLED),
        ("cancelled_by_operator", CANCELLED),
        ("unfilled", UNFILLED),
        ("something_else", EXTERNAL),
    ):
        events: list = [
            CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.60")),
            EntryClosed(entry_id=ENTRY_ID, initiator=initiator),
        ]
        entry = fold(events).entries[ENTRY_ID]
        assert classify(entry) == expected, f"{initiator!r} should classify as {expected!r}"


def test_tc_cls_07_stale_terminal_initiator_set_matches_the_skip_initiators():
    """Drift guard (CLS-03(a)/(a2)): if a new pre-fill terminal initiator is
    ever added to `_TERMINAL_SKIP_INITIATOR` (execute_entry.py) without also
    adding it to `STALE_TERMINAL_INITIATORS` (projection.py), the projection
    will NOT reopen an entry whose terminal is invalidated by a late fill --
    leaving a REAL position invisible to the stop-fill watcher, LEX, the EOD
    force-close, Flatten All and the operator's Close button (the v1.87
    regression class this whole rule exists to prevent). This used to be a
    module-level `assert` in execute_entry.py, which is wrong in production
    code: it is stripped under `python -O`, and if it ever fired it would be
    an AssertionError at IMPORT time, taking the whole panel down with a poor
    diagnostic. A proper test surfaces the same drift, loudly, in CI."""
    assert STALE_TERMINAL_INITIATORS == {"cancelled"} | set(_TERMINAL_SKIP_INITIATOR.values())


def test_tc_cls_07_close_entry_rejects_stale_terminal_initiators():
    """Fix 3 (MEDIUM, 2026-07-25 Opus DO-NOT-SHIP finding): `CloseEntry.close()`
    must REJECT "unfilled" and "cancelled_by_operator" as initiators. Both
    are journaled DIRECTLY by execute_entry.py's `_skip` as pre-fill
    terminals -- `_skip` appends `EntryClosed` itself and never routes
    through `CloseEntry.close()` at all, so nothing legitimate needs them
    accepted here. Both are also members of `STALE_TERMINAL_INITIATORS`
    (domain/projection.py): a `CondorFilled` arriving after either one is
    treated as broker truth superseding a STALE terminal and REOPENS the
    entry. Accepting either one as a valid `CloseEntry.close()` initiator
    would make it legal to run a full close (real buy-to-close broker
    orders) under an initiator a later duplicate/late fill could silently
    reopen -- something that must never happen to a genuine CLS close."""
    import asyncio

    from meic.application.close_entry import VALID_INITIATORS

    assert "unfilled" not in VALID_INITIATORS
    assert "cancelled_by_operator" not in VALID_INITIATORS

    events: list = []
    close_entry = CloseEntry(_CancelBroker(), events)

    for bad_initiator in ("unfilled", "cancelled_by_operator"):
        try:
            asyncio.run(close_entry.close(
                "e1", bad_initiator, resting_stop_ids={}, live_legs=[], close_price=D("0")))
            raise AssertionError(f"{bad_initiator!r} should be rejected as a close initiator")
        except ValueError as e:
            assert bad_initiator in str(e)
    assert events == []  # nothing was journaled for either rejected attempt


# NOTE (2026-07-25): `test_tc_cls_07_no_consumer_hardcodes_a_stale_terminal_initiator_literal`,
# a source-text-scanning regex test guarding against a THIRD consumer
# hardcoding a stale-terminal-initiator literal instead of importing
# `STALE_TERMINAL_INITIATORS`, was REMOVED here. A reviewer proved it was
# INERT: run against the pre-fix buggy `lex_ladder_watchdog.py`
# (`getattr(e, "initiator", None) != "cancelled"`), the regex found NO
# offenders -- `"initiator"` there is a quoted string ARGUMENT to `getattr`,
# not a `.initiator` attribute access, so the `\.initiator` anchor never
# matched it. It also false-negatived on single quotes, the other two set
# members ("unfilled", "cancelled_by_operator"), tuple/set literals, and a
# comparison split across lines -- exactly how the fixed code is actually
# formatted. A deleted test is honest; an inert one that looks like coverage
# but catches nothing is worse than no test at all (see this codebase's own
# `wiring_registry.py` comments on curated false positives vs silently
# broken heuristics). Real, non-evadable coverage for this exact drift now
# lives in `tests/application/test_lex_ladder_watchdog.py`'s
# `test_any_stale_terminal_initiator_never_blinds_a_genuine_stop_out` -- a
# BEHAVIOURAL pin on `_pending_ladder_starts` itself, parametrized over the
# live `STALE_TERMINAL_INITIATORS` set, proven (by temporarily reverting
# `lex_ladder_watchdog.py` to its old buggy comparison) to actually fail
# against the bug this test was meant to catch.
