"""TC-RPT-09 — RPT-15 EOD broker reconcile-and-correct (operator's zero-drift
rule). `ReportReconciler` accepts a narrow, duck-typed read-only broker
facade (never a BrokerGateway) and appends `DayBrokerConfirmed` on a match,
`CorrectionRecord` per diverging field on a mismatch (plus a critical
alert), and nothing at all when the broker is unreachable -- the day stays
bot-computed and is retried at the next boot/reconcile, never auto-confirmed.
"""
import asyncio
from decimal import Decimal as D

from pytest_bdd import given, parsers, scenarios, then

from meic.application.report_reconciler import ReportReconciler
from meic.domain.events import CondorFilled, CorrectionRecord, DayBrokerConfirmed, EntryClosed
from meic.reporting.corrections import corrected_value, corrections_for_day
from meic.reporting.trust import trust_stamp

scenarios("../features/TC-RPT-09.feature")

DAY = "2026-07-09"


class _FakeAlerts:
    def __init__(self) -> None:
        self.alerts: list[dict] = []

    def alert(self, level, message, **context) -> None:
        self.alerts.append({"level": level, "message": message, "context": context})


class _MatchingBroker:
    """Read-only facade; caller sets whatever numbers it wants returned."""

    def __init__(self, *, positions=(), fills=(), cash_delta=D("0"), fees=D("0")) -> None:
        self._positions, self._fills = positions, fills
        self._cash_delta, self._fees = cash_delta, fees

    async def positions(self):
        return list(self._positions)

    async def day_fills(self, day):
        return list(self._fills)

    async def cash_and_fees(self, day):
        return self._cash_delta, self._fees


class _UnreachableBroker:
    async def positions(self):
        raise ConnectionError("broker unreachable")

    async def day_fills(self, day):
        raise ConnectionError("broker unreachable")

    async def cash_and_fees(self, day):
        raise ConnectionError("broker unreachable")


def _bot_events(*, fee: D) -> list:
    """One filled, fully-closed entry: net_credit 4.00, the given per-share
    fee, closed same day (flat=True), one fill (fill_count=1)."""
    return [
        CondorFilled(entry_id=f"{DAY}#1", net_credit=D("4.00"), fee=fee),
        EntryClosed(entry_id=f"{DAY}#1", initiator="eod"),
    ]


# --- A matching day is stamped broker-confirmed ------------------------------

@given("the day's projected fills, cash delta, fees, and flat check match the broker",
       target_fixture="reconcile_world")
def _():
    events = _bot_events(fee=D("2.20"))  # -> $220.00 bot-side fees
    # bot net = pnl * 100 = (4.00 credit - 2.20 fee) * 100 = $180.00
    broker = _MatchingBroker(positions=(), fills=[object()], cash_delta=D("180.00"),
                             fees=D("220.00"))
    alerts = _FakeAlerts()
    reconciler = ReportReconciler(broker=broker, events=events, alerts=alerts,
                                  now=lambda: "2026-07-09T16:20:00-04:00")
    outcome = asyncio.run(reconciler.reconcile_day(DAY))
    return {"events": events, "outcome": outcome, "alerts": alerts}


@then("the day is stamped broker-confirmed and UI-25 shows the tick")
def _(reconcile_world):
    events, outcome = reconcile_world["events"], reconcile_world["outcome"]
    assert outcome.status == "confirmed"
    assert any(isinstance(e, DayBrokerConfirmed) and e.date == DAY for e in events)
    trust = trust_stamp(events, (DAY,))
    assert trust.status == "broker-confirmed"


# --- A mismatch corrects to broker truth, never silently ---------------------

@given(parsers.parse("the broker reports fees {broker_fee} where the projection "
                     "assumed {bot_fee}"), target_fixture="mismatch_world")
def _(broker_fee, bot_fee):
    bot_fee_dollars = D(bot_fee) * 100      # 2.20/share -> $220.00
    broker_fee_dollars = D(broker_fee) * 100  # 2.40/share -> $240.00
    events = _bot_events(fee=D(bot_fee))
    # bot net = (4.00 credit - bot_fee) * 100 -- matches the broker's cash_delta
    # so ONLY "fees" mismatches, isolating the scenario to that one field.
    bot_net = (D("4.00") - D(bot_fee)) * 100
    broker = _MatchingBroker(positions=(), fills=[object()], cash_delta=bot_net,
                             fees=broker_fee_dollars)
    alerts = _FakeAlerts()
    reconciler = ReportReconciler(broker=broker, events=events, alerts=alerts,
                                  now=lambda: "2026-07-09T16:20:00-04:00")
    outcome = asyncio.run(reconciler.reconcile_day(DAY))
    return {"events": events, "outcome": outcome, "alerts": alerts,
            "bot_fee_dollars": bot_fee_dollars, "broker_fee_dollars": broker_fee_dollars}


@then("a CorrectionRecord event enters the log storing both values and the diff")
def _(mismatch_world):
    outcome = mismatch_world["outcome"]
    assert outcome.status == "corrected"
    fee_corrections = [c for c in outcome.corrections if c.field == "fees"]
    assert len(fee_corrections) == 1
    rec = fee_corrections[0]
    assert D(rec.bot_value) == mismatch_world["bot_fee_dollars"]
    assert D(rec.broker_value) == mismatch_world["broker_fee_dollars"]
    assert D(rec.diff) == mismatch_world["broker_fee_dollars"] - mismatch_world["bot_fee_dollars"]
    assert any(isinstance(e, CorrectionRecord) and e.field == "fees"
               for e in mismatch_world["events"])


@then("the dashboard renders the broker value with the correction visible in the drill-down")
def _(mismatch_world):
    events = mismatch_world["events"]
    rendered = corrected_value(events, DAY, "fees", mismatch_world["bot_fee_dollars"])
    assert rendered == mismatch_world["broker_fee_dollars"]  # broker truth wins
    drilldown = corrections_for_day(events, DAY)
    assert any(c.field == "fees" and D(c.bot_value) == mismatch_world["bot_fee_dollars"]
               and D(c.broker_value) == mismatch_world["broker_fee_dollars"]
               for c in drilldown)  # both values visible side by side


@then("an alert fires and the RPT-08 correction count increments")
def _(mismatch_world):
    alerts = mismatch_world["alerts"]
    assert any(a["level"] == "critical" for a in alerts.alerts)
    correction_count = len(corrections_for_day(mismatch_world["events"], DAY))
    assert correction_count >= 1


# --- No dashboard number ever changes without a CorrectionRecord -------------

@then("any divergence between rendered numbers and the projection fold is a test failure")
def _():
    events = _bot_events(fee=D("2.20"))
    fold_value = D("220.00")
    # No CorrectionRecord for this day/field -> the rendered value is EXACTLY
    # the plain fold, never silently adjusted.
    assert corrected_value(events, DAY, "fees", fold_value) == fold_value

    # A CorrectionRecord for a DIFFERENT field leaves "fees" untouched -- a
    # correction is per-field, never a blanket override.
    events.append(CorrectionRecord(date=DAY, field="cash_delta", bot_value="400.00",
                                   broker_value="401.00", diff="1.00",
                                   at="2026-07-09T16:20:00-04:00"))
    assert corrected_value(events, DAY, "fees", fold_value) == fold_value


# --- Broker unreachable never auto-confirms ----------------------------------

@given("the EOD reconcile fetch fails", target_fixture="unreachable_world")
def _():
    events = _bot_events(fee=D("2.20"))
    reconciler = ReportReconciler(broker=_UnreachableBroker(), events=events,
                                  now=lambda: "2026-07-09T16:20:00-04:00")
    before = list(events)
    outcome = asyncio.run(reconciler.reconcile_day(DAY))
    return {"events": events, "outcome": outcome, "before": before, "reconciler": reconciler}


@then("the day remains bot-computed and reconciliation retries at the next boot or reconcile")
def _(unreachable_world):
    outcome, events, before = (unreachable_world["outcome"], unreachable_world["events"],
                               unreachable_world["before"])
    assert outcome.status == "unreachable"
    assert events == before  # NOTHING appended -- never auto-confirmed
    trust = trust_stamp(events, (DAY,))
    assert trust.status == "bot-computed"
    # Retrying later (e.g. next boot/tick) is just calling reconcile_day again --
    # no persisted "gave up" flag blocks it.
    outcome2 = asyncio.run(unreachable_world["reconciler"].reconcile_day(DAY))
    assert outcome2.status == "unreachable"
