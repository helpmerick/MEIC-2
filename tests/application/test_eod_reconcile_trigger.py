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
