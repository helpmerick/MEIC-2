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
