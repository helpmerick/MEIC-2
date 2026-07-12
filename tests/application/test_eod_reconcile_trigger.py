"""RPT-15's EOD reconcile trigger (`_maybe_eod_reconcile_once`, factored out
of live_app's health loop exactly like `_supervise_once` is for the day
supervisor -- see tests/application/test_day_supervisor.py) + the
`_BrokerReadFacade` that is the ONLY thing the reconciler is ever handed.
"""
import asyncio
import types
from datetime import datetime, time as dtime, timezone
from decimal import Decimal as D

from meic.adapters.api.server import EOD_RECONCILE_TIME, _BrokerReadFacade, _maybe_eod_reconcile_once
from meic.application.report_reconciler import ReportReconciler
from meic.domain.events import (
    CondorFilled,
    CorrectionRecord,
    DayArmed,
    DayBrokerConfirmed,
    EntryClosed,
    FilledLeg,
    SettlementRecorded,
)

DAY = "2026-07-09"


def _app_state():
    return types.SimpleNamespace()


class _StubBroker:
    """A minimal stand-in with submit/replace/cancel present -- proving the
    FACADE (not the broker itself) is what makes the reconciler read-only."""

    def __init__(self, *, positions=(), fills=(), cash_delta=D("0"), fees=D("0")):
        self._positions, self._fills = positions, fills
        self._cash_delta, self._fees = cash_delta, fees
        self.submit_called = False

    async def submit(self, order):  # pragma: no cover -- must never be reached via the facade
        self.submit_called = True

    async def positions(self):
        return list(self._positions)

    async def day_fills(self, day):
        return list(self._fills)

    async def cash_and_fees(self, day):
        return self._cash_delta, self._fees


def _events(*, confirmed=False, corrected=False):
    events = [DayArmed(date=DAY, entry_count=1),
              CondorFilled(entry_id=f"{DAY}#1", net_credit=D("4.00")),
              EntryClosed(entry_id=f"{DAY}#1", initiator="eod")]  # flat by EOD
    if confirmed:
        events.append(DayBrokerConfirmed(date=DAY, at="t"))
    if corrected:
        events.append(CorrectionRecord(date=DAY, field="fees", bot_value="0",
                                       broker_value="1", diff="1", at="t"))
    return events


def test_broker_read_facade_forwards_only_the_three_read_calls():
    broker = _StubBroker(positions=("p",), fills=("f",), cash_delta=D("1"), fees=D("2"))
    facade = _BrokerReadFacade(broker)
    assert asyncio.run(facade.positions()) == ["p"]
    assert asyncio.run(facade.day_fills(DAY)) == ["f"]
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
    broker = _StubBroker(positions=(), fills=[object()], cash_delta=D("400.00"), fees=D("0"))
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


def test_an_already_corrected_day_is_never_re_reconciled():
    events = _events(corrected=True)
    before = list(events)
    reconciler = ReportReconciler(broker=_StubBroker(), events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn))
    assert events == before


def test_unreachable_broker_appends_nothing_and_will_retry_next_tick():
    events = _events()

    class _Unreachable:
        async def positions(self):
            raise ConnectionError("down")

        async def day_fills(self, day):
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
    broker = _StubBroker(positions=(), fills=[object()], cash_delta=D("400.00"), fees=D("0"))
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
        CondorFilled(entry_id=f"{DAY}#1", net_credit=D("3.60"), fee=D("0.0488"), legs=(leg,)),
    ]
    settlement_row = types.SimpleNamespace(
        symbol="SPXW  260709C07540000", transaction_sub_type="Cash Settled Assignment",
        value=D("-364.0"), net_value=D("-369.0"), price=D("7540.0"), quantity=D("1"),
        executed_at=datetime(2026, 7, 10, 2, 0, tzinfo=timezone.utc))
    # NOTE: ReportReconciler compares str(Decimal) -- scale-sensitive, not just
    # value-equal (a PRE-EXISTING characteristic, not introduced here). The
    # bot's own arithmetic naturally lands on 4 decimal places here (fee
    # 0.0488 has scale 4), so the "matching" broker figures below are
    # expressed at that SAME scale to actually exercise a true match.
    broker = _SettlementBroker(positions=(), fills=[object()], cash_delta=D("-13.8800"),
                               fees=D("9.8800"), settlements=[settlement_row])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "2026-07-09T16:20:00-04:00")
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)

    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn, broker_reads=broker))

    assert any(isinstance(e, SettlementRecorded) and e.value == D("-369.0") for e in events)
    assert any(isinstance(e, DayBrokerConfirmed) for e in events)  # settled net matched broker truth


def test_a_capture_failure_never_crashes_the_tick():
    events = _events()

    class _BrokenSettlements(_StubBroker):
        async def day_settlements(self, day):
            raise ConnectionError("down")

    broker = _BrokenSettlements(positions=(), fills=[object()], cash_delta=D("400.00"), fees=D("0"))
    reconciler = ReportReconciler(broker=broker, events=events)
    comp = types.SimpleNamespace(events=events)
    now_fn = lambda: datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    asyncio.run(_maybe_eod_reconcile_once(_app_state(), comp, reconciler, now_fn, broker_reads=broker))
    # The reconcile itself still ran (capture's failure was swallowed) --
    # RPT-15's existing behavior for this bot/broker shape is unaffected.
    assert any(isinstance(e, DayBrokerConfirmed) for e in events)
