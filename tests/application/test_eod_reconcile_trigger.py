"""RPT-15's EOD reconcile trigger (`_maybe_eod_reconcile_once`, factored out
of live_app's health loop exactly like `_supervise_once` is for the day
supervisor -- see tests/application/test_day_supervisor.py) + the
`_BrokerReadFacade` that is the ONLY thing the reconciler is ever handed.

OWN-01/OWN-03 (2026-07-11 incident fix): `ReportReconciler.reconcile_day` no
longer takes `cash_delta`/`fees` straight off the broker's whole-account
`cash_and_fees(day)` -- it derives them from `day_fills`/`day_settlements`
rows scoped to the bot's OWN journaled order ids (see
application/report_reconciler.py's module docstring for the incident this
fixes). Every fake broker below therefore needs a real `day_fills`/
`day_settlements` row shape (order_id / symbol / signed `value` / signed
`net_value`, the row's fee being exactly `value - net_value`) instead of a
directly-fed cash_delta/fees pair, and every bot-side `CondorFilled` needs
its own `broker_order_id` journaled so the reconciler recognises the fake
fill as its own.
"""
import asyncio
import types
from datetime import datetime, time as dtime, timezone
from decimal import Decimal as D

from meic.adapters.api.server import (
    EOD_RECONCILE_TIME,
    _BrokerReadFacade,
    _mark_expired_sides,
    _maybe_eod_reconcile_once,
    _settlement_lookback_days,
)
from meic.application.report_reconciler import ReportReconciler
from meic.domain.events import (
    CondorFilled,
    CorrectionRecord,
    DayArmed,
    DayBrokerConfirmed,
    EntryClosed,
    FilledLeg,
    SettlementRecorded,
    ShortStopped,
    SideClosed,
    SideExpired,
)
from meic.domain.projection import fold

DAY = "2026-07-09"
ORDER_ID = "482621396"  # the bot's own entry order id (OWN-01/OWN-03)


def _app_state():
    return types.SimpleNamespace()


def _fill(order_id, net_value, *, value=None, symbol="SPXW  260709C07540000"):
    """A fake broker Trade-row transaction: just enough shape for
    `report_reconciler.py`'s field readers (`_order_id_of`/`_symbol_of`/
    `_net_value_of`/`_fee_cost_of`). The row's FEE is `value - net_value`
    (the broker's own `net_value = value + fees` invariant, fees negative --
    operator-verified against the real 2026-07-10 rows), never a separate
    fee-category field. `value=None` defaults to a zero-fee row."""
    return types.SimpleNamespace(order_id=order_id, symbol=symbol, net_value=net_value,
                                 value=net_value if value is None else value)


class _StubBroker:
    """A minimal stand-in with submit/replace/cancel present -- proving the
    FACADE (not the broker itself) is what makes the reconciler read-only."""

    def __init__(self, *, positions=(), fills=(), settlements=(), cash_delta=D("0"), fees=D("0")):
        self._positions, self._fills, self._settlements = positions, fills, settlements
        self._cash_delta, self._fees = cash_delta, fees
        self.submit_called = False

    async def submit(self, order):  # pragma: no cover -- must never be reached via the facade
        self.submit_called = True

    async def positions(self):
        return list(self._positions)

    async def day_fills(self, day):
        return list(self._fills)

    async def day_settlements(self, day):
        return list(self._settlements)

    async def cash_and_fees(self, day):
        return self._cash_delta, self._fees


def _events(*, confirmed=False, corrected=False, fee=D("0"), scope=None):
    events = [DayArmed(date=DAY, entry_count=1),
              CondorFilled(entry_id=f"{DAY}#1", net_credit=D("4.00"), fee=fee,
                           broker_order_id=ORDER_ID),
              EntryClosed(entry_id=f"{DAY}#1", initiator="eod")]  # flat by EOD
    if confirmed:
        events.append(DayBrokerConfirmed(date=DAY, at="t"))
    if corrected:
        events.append(CorrectionRecord(date=DAY, field="fees", bot_value="0",
                                       broker_value="1", diff="1", at="t", scope=scope))
    return events


def test_broker_read_facade_forwards_only_the_four_read_calls():
    broker = _StubBroker(positions=("p",), fills=("f",), settlements=("s",),
                         cash_delta=D("1"), fees=D("2"))
    facade = _BrokerReadFacade(broker)
    assert asyncio.run(facade.positions()) == ["p"]
    assert asyncio.run(facade.day_fills(DAY)) == ["f"]
    assert asyncio.run(facade.day_settlements(DAY)) == ["s"]
    assert asyncio.run(facade.cash_and_fees(DAY)) == (D("1"), D("2"))
    assert not hasattr(facade, "submit")
    assert not hasattr(facade, "replace")
    assert not hasattr(facade, "cancel")
    assert broker.submit_called is False


def test_before_eod_time_does_nothing():
    events = _events()
    reconciler = ReportReconciler(broker=_StubBroker(), events=events)
    comp = types.SimpleNamespace(events=events)
    before = list(events)
    now_fn = lambda: datetime(2026, 7, 9, 15, 0, tzinfo=timezone.utc)  # before 16:15
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert events == before


def test_no_activity_day_is_skipped():
    events: list = []  # no DayArmed/CondorFilled/EntrySkipped -- not a trading day
    reconciler = ReportReconciler(broker=_StubBroker(), events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert events == []


def test_a_trading_day_past_eod_time_reconciles_once():
    events = _events()
    broker = _StubBroker(positions=(), fills=[_fill(ORDER_ID, D("400.00"))])
    reconciler = ReportReconciler(broker=broker, events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert any(isinstance(e, DayBrokerConfirmed) for e in events)


def test_an_already_confirmed_day_is_never_re_reconciled():
    events = _events(confirmed=True)
    before = list(events)
    reconciler = ReportReconciler(broker=_StubBroker(), events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert events == before  # no duplicate DayBrokerConfirmed appended


def test_an_own_scoped_corrected_day_is_never_re_reconciled():
    """A `CorrectionRecord` written by the OWN-01/OWN-03-scoped reconciler
    (`scope="own"`) IS a genuine resolution -- the gate must skip the day."""
    events = _events(corrected=True, scope="own")
    before = list(events)
    reconciler = ReportReconciler(broker=_StubBroker(), events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert events == before


def test_a_legacy_corrected_day_with_no_scope_is_re_reconciled():
    """2026-07-12 own-scoping fix: a `CorrectionRecord` with NO `scope`
    (or `scope != "own"`) is a LEGACY record from before the OWN-01/OWN-03
    fix -- written when the reconciler summed the operator's WHOLE shared
    account into "broker truth" (the real 2026-07-10 incident). It is not a
    resolution, so the gate must NOT treat the day as already-reconciled:
    the tick reruns it and, on broker rows that now agree, lands a fresh
    `DayBrokerConfirmed` alongside the stale legacy record."""
    events = _events(corrected=True, scope=None)
    broker = _StubBroker(positions=(), fills=[_fill(ORDER_ID, D("400.00"))])
    reconciler = ReportReconciler(broker=broker, events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert any(isinstance(e, DayBrokerConfirmed) for e in events)


def test_unreachable_broker_appends_nothing_and_will_retry_next_tick():
    events = _events()

    class _Unreachable:
        async def positions(self):
            raise ConnectionError("down")

        async def day_fills(self, day):
            raise ConnectionError("down")

        async def day_settlements(self, day):
            raise ConnectionError("down")

        async def cash_and_fees(self, day):
            raise ConnectionError("down")

    reconciler = ReportReconciler(broker=_Unreachable(), events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    before = list(events)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert events == before  # nothing appended
    # A later tick (still no DayBrokerConfirmed/CorrectionRecord) retries.
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert events == before


def test_eod_reconcile_time_is_after_settlement():
    assert EOD_RECONCILE_TIME == dtime(16, 15)


# --- EOD-01 v1.59: settlement capture runs BEFORE the reconcile compare -----

class _SettlementBroker(_StubBroker):
    """Adds `day_settlements` -- the surface `capture_settlements` reads."""

    def __init__(self, *, settlements=(), **kw):
        super().__init__(**kw)
        self._settlements = settlements

    async def day_settlements(self, day):
        return list(self._settlements)


def test_broker_reads_none_skips_capture_entirely_unchanged_behavior():
    """Every pre-v1.59 caller (and every test above) omits `broker_reads` --
    must behave EXACTLY as before: no capture attempted, no crash."""
    events = _events()
    broker = _StubBroker(positions=(), fills=[_fill(ORDER_ID, D("400.00"))])
    reconciler = ReportReconciler(broker=broker, events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert not any(isinstance(e, SettlementRecorded) for e in events)
    assert any(isinstance(e, DayBrokerConfirmed) for e in events)


def test_settlement_capture_runs_before_the_reconcile_compare():
    """A held-to-expiry entry (no EntryClosed) with a matching broker
    settlement: capture must land a SettlementRecorded BEFORE reconcile_day
    runs, so the day's true (settled) net is what gets checked."""
    leg = FilledLeg(symbol="SPXW  260709C07540000", right="C", role="short", qty=1, price=D("2.15"))
    events = [
        DayArmed(date=DAY, entry_count=1),
        CondorFilled(entry_id=f"{DAY}#1", net_credit=D("3.60"), fee=D("0.0488"), legs=(leg,),
                     broker_order_id=ORDER_ID),
    ]
    # OWN-01/OWN-03: the Trade-side fill and its Receive-Deliver settlement
    # both carry the bot's own order id / matching symbol, so the reconciler
    # recognises them as its own. Each row's FEE is `value - net_value` (the
    # broker's own invariant -- no fee-category field is read at all): the
    # fill's 360.00 - 355.12 = 4.88, the settlement's -364.00 - (-369.00) =
    # 5.00, together the 9.88 the bot's own fold also computes. Scale-2, as
    # the real broker returns them -- `_agrees` compares NUMBERS, so the
    # bot's scale-4 "9.8800" and the broker's "9.88" correctly agree.
    fill = _fill(ORDER_ID, D("355.12"), value=D("360.00"))
    settlement_row = types.SimpleNamespace(
        symbol="SPXW  260709C07540000", transaction_sub_type="Cash Settled Assignment",
        value=D("-364.00"), net_value=D("-369.00"), price=D("7540.0"), quantity=D("1"),
        executed_at=datetime(2026, 7, 10, 2, 0, tzinfo=timezone.utc))
    broker = _SettlementBroker(positions=(), fills=[fill], settlements=[settlement_row])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "2026-07-09T16:20:00-04:00")
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)

    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn, broker_reads=broker))

    assert any(isinstance(e, SettlementRecorded) and e.value == D("-369.0") for e in events)
    assert any(isinstance(e, DayBrokerConfirmed) for e in events)  # settled net matched broker truth


def test_a_capture_failure_never_crashes_the_tick():
    """`capture_settlements`'s own failure is swallowed (no crash, no
    SettlementRecorded). OWN-01/OWN-03 fix: `reconcile_day` now ALSO reads
    `day_settlements` (to scope settlement rows to the bot's own symbols),
    on the SAME broker object -- so a broker whose `day_settlements` is down
    makes the reconcile itself "unreachable" too (retried next tick), not a
    silent whole-account fallback. This is a real behavior change from the
    pre-fix reconciler (which never touched `day_settlements` and so stayed
    unaffected by this failure) -- and a correct one: a reconcile that can't
    see the day's settlements can no longer safely confirm the day flat."""
    events = _events()

    class _BrokenSettlements(_StubBroker):
        async def day_settlements(self, day):
            raise ConnectionError("down")

    broker = _BrokenSettlements(positions=(), fills=[_fill(ORDER_ID, D("400.00"))])
    reconciler = ReportReconciler(broker=broker, events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    before = list(events)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn, broker_reads=broker))
    # Capture's own failure never crashed the tick, and nothing was appended --
    # the day stays bot-computed and retries at the next tick, same as any
    # other broker-unreachable case.
    assert events == before
    assert not any(isinstance(e, SettlementRecorded) for e in events)
    assert not any(isinstance(e, DayBrokerConfirmed) for e in events)


# --- 2026-07-13 look-back fix: a late settlement must NEVER be lost forever --
#
# Root cause: SPX 0DTE settlements post to the broker's Receive-Deliver
# ledger the day AFTER the trading day (settlement_capture.py's own module
# docstring). The ordinary 16:15 same-day capture above therefore routinely
# finds nothing, `capture_settlements` appends zero rows, and the day still
# gets sealed by a `DayBrokerConfirmed`/own-scoped `CorrectionRecord` at that
# same tick (or an earlier one). With no look-back, `already` then blocks
# that day FOREVER and the settlement that posts tomorrow is never captured.
# These tests drive `_maybe_eod_reconcile_once` directly (never a real
# broker), so a fake reconciler that only RECORDS which days it was asked to
# reconcile is precise about exactly what this trigger orchestrates, leaving
# `ReportReconciler`'s own compare logic (covered above) out of scope here.

DAY0 = "2026-07-06"  # oldest prior day used below
DAY1 = "2026-07-07"
DAY2 = "2026-07-08"
TODAY = "2026-07-10"  # the tick's "now" -- strictly after DAY/DAY0-2


class _SpyReconciler:
    """Records every day it is asked to reconcile -- lets these tests assert
    exactly which days got re-reconciled without depending on
    `ReportReconciler`'s own broker-truth compare semantics."""

    def __init__(self):
        self.calls: list[str] = []

    async def reconcile_day(self, day: str) -> None:
        self.calls.append(day)


class _MultiDaySettlementBroker(_StubBroker):
    """`day_settlements` keyed per-day, with per-day call tracking and the
    ability to make specific days raise -- everything the look-back tests
    below need that `_SettlementBroker` (single-day) doesn't give them."""

    def __init__(self, *, settlements_by_day=None, raise_days=frozenset(), **kw):
        super().__init__(**kw)
        self._settlements_by_day = settlements_by_day or {}
        self._raise_days = frozenset(raise_days)
        self.day_settlements_calls: list[str] = []

    async def day_settlements(self, day):
        self.day_settlements_calls.append(day)
        if day in self._raise_days:
            raise ConnectionError("down")
        return list(self._settlements_by_day.get(day, ()))


def _unresolved_short_entry(day: str, *, symbol: str) -> list:
    """A day whose only entry has a short leg never stopped/closed --
    `EntryProjection.settlement_pending` is True until a `SettlementRecorded`
    lands for `symbol` (see domain/projection.py)."""
    leg = FilledLeg(symbol=symbol, right="C", role="short", qty=1, price=D("2.15"))
    return [DayArmed(date=day, entry_count=1),
            CondorFilled(entry_id=f"{day}#1", net_credit=D("4.00"), fee=D("0.05"),
                        legs=(leg,), broker_order_id=f"order-{day}")]


def _settlement_row(symbol: str, *, value=D("-2.00"), net_value=D("-2.05")):
    return types.SimpleNamespace(
        symbol=symbol, transaction_sub_type="Cash Settled Assignment",
        value=value, net_value=net_value, price=D("7540.0"), quantity=D("1"),
        executed_at=datetime(2026, 7, 8, 2, 0, tzinfo=timezone.utc))


def test_the_real_bug_a_late_settlement_is_captured_and_the_day_re_reconciled():
    """THE BUG, reproduced: DAY's entry is still `settlement_pending` (short
    leg never stopped, no `SettlementRecorded` yet) but an EARLIER tick
    already sealed the day with a `DayBrokerConfirmed` -- exactly the state
    every entry in the real journal is stuck in. A LATER tick, whose broker
    NOW has DAY's settlement row available, must capture it (append a
    `SettlementRecorded`) AND re-reconcile DAY -- the `already` gate must not
    freeze it forever. Against pre-fix code this fails: the look-back does
    not exist, so nothing ever looks back at DAY again."""
    symbol = "SPXW  260707C07540000"
    events = [*_unresolved_short_entry(DAY1, symbol=symbol),
             DayBrokerConfirmed(date=DAY1, at="2026-07-07T16:20:00-04:00")]
    broker = _MultiDaySettlementBroker(settlements_by_day={DAY1: [_settlement_row(symbol)]})
    reconciler = _SpyReconciler()
    comp = types.SimpleNamespace(events=events)
    later_now_fn = lambda: datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)  # a LATER tick, DAY1's "tomorrow"

    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, later_now_fn,
                                          broker_reads=broker, lookback_days=5))

    assert any(isinstance(e, SettlementRecorded) and e.day == DAY1 for e in events)
    assert DAY1 in reconciler.calls


def test_look_back_idempotency_second_tick_appends_nothing_and_does_not_reconcile():
    """Running the look-back twice must append the settlement only once
    (`capture_settlements`'s own idempotency) and must NOT re-reconcile the
    second time -- nothing new landed, so there is nothing to re-check."""
    symbol = "SPXW  260707C07540000"
    events = [*_unresolved_short_entry(DAY1, symbol=symbol),
             DayBrokerConfirmed(date=DAY1, at="2026-07-07T16:20:00-04:00")]
    broker = _MultiDaySettlementBroker(settlements_by_day={DAY1: [_settlement_row(symbol)]})
    reconciler = _SpyReconciler()
    comp = types.SimpleNamespace(events=events)
    later_now_fn = lambda: datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)

    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, later_now_fn,
                                          broker_reads=broker, lookback_days=5))
    assert reconciler.calls.count(DAY1) == 1
    assert sum(1 for e in events if isinstance(e, SettlementRecorded)) == 1

    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, later_now_fn,
                                          broker_reads=broker, lookback_days=5))
    assert sum(1 for e in events if isinstance(e, SettlementRecorded)) == 1  # still just one
    assert reconciler.calls.count(DAY1) == 1  # not re-reconciled a second time


def test_a_day_with_no_settlement_pending_entries_is_not_re_fetched_at_all():
    """A prior day whose entry already closed some other way (no unresolved
    short) has nothing outstanding -- `_has_settlement_pending` says so from
    the log alone, and the broker's `day_settlements` must never even be
    called for it (no wasted broker round-trip, no stamp churn)."""
    events = [DayArmed(date=DAY1, entry_count=1),
             CondorFilled(entry_id=f"{DAY1}#1", net_credit=D("4.00"), fee=D("0"),
                          broker_order_id="order-closed"),
             EntryClosed(entry_id=f"{DAY1}#1", initiator="eod"),  # flat by EOD -- nothing pending
             DayBrokerConfirmed(date=DAY1, at="2026-07-07T16:20:00-04:00")]
    broker = _MultiDaySettlementBroker(settlements_by_day={})
    reconciler = _SpyReconciler()
    comp = types.SimpleNamespace(events=events)
    later_now_fn = lambda: datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)

    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, later_now_fn,
                                          broker_reads=broker, lookback_days=5))

    assert DAY1 not in broker.day_settlements_calls
    assert reconciler.calls == []


def test_the_ordinary_case_is_unchanged_no_stamp_churn_every_tick():
    """A prior day that has ALREADY been fully captured (its settlement is
    already journaled) must not be re-reconciled on every subsequent tick --
    only a NEWLY captured settlement earns a re-reconcile."""
    symbol = "SPXW  260707C07540000"
    events = [*_unresolved_short_entry(DAY1, symbol=symbol),
             SettlementRecorded(entry_id=f"{DAY1}#1", day=DAY1, at="2026-07-08T02:00:00+00:00",
                                symbol=symbol, sub_type="Cash Settled Assignment",
                                quantity=1, price=D("7540.0"), value=D("-2.05"), fee=None,
                                source="tastytrade_receive_deliver"),
             DayBrokerConfirmed(date=DAY1, at="2026-07-07T16:20:00-04:00")]
    broker = _MultiDaySettlementBroker(settlements_by_day={DAY1: [_settlement_row(symbol)]})
    reconciler = _SpyReconciler()
    comp = types.SimpleNamespace(events=events)
    later_now_fn = lambda: datetime(2026, 7, 8, 20, 0, tzinfo=timezone.utc)

    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, later_now_fn,
                                          broker_reads=broker, lookback_days=5))

    assert reconciler.calls == []  # already settled -- no stamp churn, no alert spam


def test_look_back_is_bounded_never_walks_the_whole_journal():
    """Three prior days all have an outstanding settlement, but `lookback_days`
    is pinned to 2 -- only the two MOST RECENT prior days may be re-fetched;
    the oldest (DAY0) is never touched, proving the walk is bounded."""
    events = []
    for day in (DAY0, DAY1, DAY2):
        events += _unresolved_short_entry(day, symbol=f"SPXW  26070{day[-1]}C07540000")
        events.append(DayBrokerConfirmed(date=day, at=f"{day}T16:20:00-04:00"))
    broker = _MultiDaySettlementBroker(settlements_by_day={
        day: [_settlement_row(f"SPXW  26070{day[-1]}C07540000")] for day in (DAY0, DAY1, DAY2)})
    reconciler = _SpyReconciler()
    comp = types.SimpleNamespace(events=events)
    later_now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)

    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, later_now_fn,
                                          broker_reads=broker, lookback_days=2))

    assert DAY0 not in broker.day_settlements_calls
    assert DAY1 in broker.day_settlements_calls
    assert DAY2 in broker.day_settlements_calls


def test_a_look_back_capture_failure_for_one_day_does_not_block_others_or_crash():
    """DAY1's broker read is down; DAY2's is fine. DAY1's failure must not
    prevent DAY2 from being captured and reconciled, and must not crash."""
    symbol1 = "SPXW  260707C07540000"
    symbol2 = "SPXW  260708C07540000"
    events = [*_unresolved_short_entry(DAY1, symbol=symbol1),
             DayBrokerConfirmed(date=DAY1, at="2026-07-07T16:20:00-04:00"),
             *_unresolved_short_entry(DAY2, symbol=symbol2),
             DayBrokerConfirmed(date=DAY2, at="2026-07-08T16:20:00-04:00")]
    broker = _MultiDaySettlementBroker(
        settlements_by_day={DAY2: [_settlement_row(symbol2)]}, raise_days={DAY1})
    reconciler = _SpyReconciler()
    comp = types.SimpleNamespace(events=events)
    later_now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)

    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, later_now_fn,
                                          broker_reads=broker, lookback_days=5))

    assert not any(isinstance(e, SettlementRecorded) and e.day == DAY1 for e in events)
    assert any(isinstance(e, SettlementRecorded) and e.day == DAY2 for e in events)
    assert DAY1 not in reconciler.calls
    assert DAY2 in reconciler.calls


def test_settlement_lookback_days_dial_default_and_bounds():
    assert _settlement_lookback_days({}) == 5
    assert _settlement_lookback_days({"MEIC_SETTLEMENT_LOOKBACK_DAYS": "3"}) == 3
    assert _settlement_lookback_days({"MEIC_SETTLEMENT_LOOKBACK_DAYS": "0"}) == 5  # out of range
    assert _settlement_lookback_days({"MEIC_SETTLEMENT_LOOKBACK_DAYS": "31"}) == 5  # out of range
    assert _settlement_lookback_days({"MEIC_SETTLEMENT_LOOKBACK_DAYS": "not-a-number"}) == 5


# --- EOD-01 v1.59: `_mark_expired_sides` -- mark remaining sides EXPIRED, --
# --- but ONLY after the broker's own SettlementRecorded lands --------------
#
# THE REAL BUG (2026-07-13): the ONLY `SideExpired` emitter in the whole
# codebase was the demo simulator (composition/runtime.py) -- zero
# `SideExpired` events ever landed in the live journal. A held-to-expiry
# condor (both sides worthless -- the most common, most desirable MEIC
# outcome) never reached EntryProjection.status == "EXPIRED": it fell
# through to "PROTECTED" forever, and the frontend's TERMINAL list keeps a
# live Close button armed on a position that no longer exists.

DAY_X = "2026-07-09"


def _condor_legs():
    return (
        FilledLeg(symbol="SPXW  260709P07535000", right="P", role="short", qty=1, price=D("2.20")),
        FilledLeg(symbol="SPXW  260709P07510000", right="P", role="long", qty=1, price=D("0.40")),
        FilledLeg(symbol="SPXW  260709C07540000", right="C", role="short", qty=1, price=D("2.15")),
        FilledLeg(symbol="SPXW  260709C07565000", right="C", role="long", qty=1, price=D("0.35")),
    )


def _settled(entry_id, symbol, *, value=D("0"), sub_type="Expiration"):
    return SettlementRecorded(entry_id=entry_id, day=DAY_X, at="2026-07-10T02:00:00+00:00",
                              symbol=symbol, sub_type=sub_type, quantity=1, price=None,
                              value=value, fee=None, source="tastytrade_receive_deliver")


def test_the_real_bug_untouched_condor_both_settled_shorts_become_expired():
    """TC-EOD-01's shape: an untouched condor (neither side stopped/closed)
    whose four legs ALL get `SettlementRecorded` -- both sides must be
    marked `SideExpired` and `EntryProjection.status` must become
    `EXPIRED`. Against the pre-fix code (no `_mark_expired_sides` at all)
    this fails: status stays `PROTECTED` forever."""
    entry_id = f"{DAY_X}#1"
    events = [CondorFilled(entry_id=entry_id, net_credit=D("3.60"), fee=D("0.05"),
                          legs=_condor_legs()),
             _settled(entry_id, "SPXW  260709P07535000"),
             _settled(entry_id, "SPXW  260709P07510000"),
             _settled(entry_id, "SPXW  260709C07540000"),
             _settled(entry_id, "SPXW  260709C07565000")]

    _mark_expired_sides(events, DAY_X)

    entry = fold(events).entries[entry_id]
    assert set(entry.sides_expired) == {"PUT", "CALL"}
    assert entry.status == "EXPIRED"


def test_a_stopped_side_is_never_marked_expired_the_surviving_side_is():
    """Pins the real 2026-07-10 shape: CALL stopped, PUT settled -> only PUT
    is marked expired, and status checks `sides_stopped` first so it stays
    STOPPED (never EXPIRED) even though one side genuinely expired."""
    entry_id = f"{DAY_X}#1"
    events = [CondorFilled(entry_id=entry_id, net_credit=D("3.60"), fee=D("0.05"),
                          legs=_condor_legs()),
             ShortStopped(entry_id=entry_id, side="CALL", fill=D("3.00"), slippage=D("0.10")),
             _settled(entry_id, "SPXW  260709P07535000"),
             _settled(entry_id, "SPXW  260709P07510000")]

    _mark_expired_sides(events, DAY_X)

    entry = fold(events).entries[entry_id]
    assert entry.sides_expired == ("PUT",)
    assert entry.status == "STOPPED"


def test_never_guess_a_short_with_no_settlement_is_not_marked_expired():
    """The whole point of EOD-01 v1.59: no `SettlementRecorded` for the CALL
    short leg's symbol -> the CALL side is NOT marked expired, no matter how
    far past expiration it is. Never a guess from a clock or moneyness."""
    entry_id = f"{DAY_X}#1"
    events = [CondorFilled(entry_id=entry_id, net_credit=D("3.60"), fee=D("0.05"),
                          legs=_condor_legs()),
             _settled(entry_id, "SPXW  260709P07535000"),
             _settled(entry_id, "SPXW  260709P07510000")]
    # No settlement at all for the CALL short (SPXW 260709C07540000).

    _mark_expired_sides(events, DAY_X)

    entry = fold(events).entries[entry_id]
    assert entry.sides_expired == ("PUT",)
    assert "CALL" not in entry.sides_expired
    assert entry.status == "PROTECTED"  # still provisional -- never guessed


def test_idempotent_two_passes_append_side_expired_exactly_once_each():
    """Two EOD passes (e.g. two health ticks) over the same log must append
    `SideExpired` exactly once per side -- never a duplicate."""
    entry_id = f"{DAY_X}#1"
    events = [CondorFilled(entry_id=entry_id, net_credit=D("3.60"), fee=D("0.05"),
                          legs=_condor_legs()),
             _settled(entry_id, "SPXW  260709P07535000"),
             _settled(entry_id, "SPXW  260709P07510000"),
             _settled(entry_id, "SPXW  260709C07540000"),
             _settled(entry_id, "SPXW  260709C07565000")]

    _mark_expired_sides(events, DAY_X)
    _mark_expired_sides(events, DAY_X)  # second pass -- must be a no-op

    put_expired = [e for e in events if isinstance(e, SideExpired) and e.side == "PUT"]
    call_expired = [e for e in events if isinstance(e, SideExpired) and e.side == "CALL"]
    assert len(put_expired) == 1
    assert len(call_expired) == 1


def test_decay_closed_entry_is_never_marked_expired():
    """A whole entry closed by decay (`EntryClosed(initiator="decay")`) has
    a `close_initiator` -- not "remaining" at all, even if its shorts'
    symbols happen to carry a settlement row."""
    entry_id = f"{DAY_X}#1"
    events = [CondorFilled(entry_id=entry_id, net_credit=D("3.60"), fee=D("0.05"),
                          legs=_condor_legs()),
             EntryClosed(entry_id=entry_id, initiator="decay"),
             _settled(entry_id, "SPXW  260709P07535000"),
             _settled(entry_id, "SPXW  260709P07510000"),
             _settled(entry_id, "SPXW  260709C07540000"),
             _settled(entry_id, "SPXW  260709C07565000")]

    _mark_expired_sides(events, DAY_X)

    entry = fold(events).entries[entry_id]
    assert entry.sides_expired == ()
    assert entry.status == "DECAY_CLOSED"


def test_operator_closed_side_is_never_marked_expired():
    """A per-side operator close (`SideClosed`) takes that side out of
    "remaining" even though its short leg's symbol later settles."""
    entry_id = f"{DAY_X}#1"
    events = [CondorFilled(entry_id=entry_id, net_credit=D("3.60"), fee=D("0.05"),
                          legs=_condor_legs()),
             SideClosed(entry_id=entry_id, side="CALL"),
             _settled(entry_id, "SPXW  260709P07535000"),
             _settled(entry_id, "SPXW  260709P07510000"),
             _settled(entry_id, "SPXW  260709C07540000")]

    _mark_expired_sides(events, DAY_X)

    entry = fold(events).entries[entry_id]
    assert entry.sides_expired == ("PUT",)
    assert "CALL" not in entry.sides_expired


def test_lex_recovered_stopped_side_is_never_marked_expired():
    """A LEX-recovered side (`ShortStopped` + `LongSold`) is still a stopped
    side -- excluded the same way a plain stop is, never double-classified
    as expired."""
    from meic.domain.events import LongSold

    entry_id = f"{DAY_X}#1"
    events = [CondorFilled(entry_id=entry_id, net_credit=D("3.60"), fee=D("0.05"),
                          legs=_condor_legs()),
             ShortStopped(entry_id=entry_id, side="CALL", fill=D("3.00"), slippage=D("0.10")),
             LongSold(entry_id=entry_id, side="CALL", recovery=D("0.20")),
             _settled(entry_id, "SPXW  260709P07535000"),
             _settled(entry_id, "SPXW  260709P07510000")]

    _mark_expired_sides(events, DAY_X)

    entry = fold(events).entries[entry_id]
    assert entry.sides_expired == ("PUT",)
    assert entry.status == "LEX_RECOVERED"
