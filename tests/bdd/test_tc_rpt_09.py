"""TC-RPT-09 — RPT-15 EOD broker reconcile-and-correct (operator's zero-drift
rule). `ReportReconciler` accepts a narrow, duck-typed read-only broker
facade (never a BrokerGateway) and appends `DayBrokerConfirmed` on a match,
`CorrectionRecord` per diverging field on a mismatch (plus a critical
alert), and nothing at all when the broker is unreachable -- the day stays
bot-computed and is retried at the next boot/reconcile, never auto-confirmed.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D

from pytest_bdd import given, parsers, scenarios, then

from meic.application.report_reconciler import ReportReconciler
from meic.application.settlement_capture import capture_settlements
from meic.domain.events import (
    CondorFilled,
    CorrectionRecord,
    DayBrokerConfirmed,
    EntryClosed,
    FilledLeg,
    ForeignDetected,
    SettlementRecorded,
)
from meic.reporting.corrections import corrected_value, corrections_for_day
from meic.reporting.folds import entries_by_day, entry_dollars
from meic.reporting.trust import trust_stamp

scenarios("../features/TC-RPT-09.feature")

DAY = "2026-07-09"

# --- the real 2026-07-09 vector (EOD-01 v1.59) --------------------------------

P7535 = "SPXW  260709P07535000"
P7510 = "SPXW  260709P07510000"
C7540 = "SPXW  260709C07540000"
C7565 = "SPXW  260709C07565000"


def _real_condor_legs() -> tuple[FilledLeg, ...]:
    return (
        FilledLeg(symbol=P7535, right="P", role="short", qty=1, price=D("2.20")),
        FilledLeg(symbol=P7510, right="P", role="long", qty=1, price=D("0.40")),
        FilledLeg(symbol=C7540, right="C", role="short", qty=1, price=D("2.15")),
        FilledLeg(symbol=C7565, right="C", role="long", qty=1, price=D("0.35")),
    )


@dataclass
class _FakeSettlement:
    """Mirrors the tastytrade SDK's Receive-Deliver Transaction shape closely
    enough for `capture_settlements`'s field reads (see
    application/backfill.py's docstring for the exact SDK field mapping this
    mirrors)."""
    symbol: str
    transaction_sub_type: str
    value: D
    net_value: D
    price: D | None = None
    quantity: D = D("1")
    executed_at: datetime = datetime(2026, 7, 10, 2, 0, tzinfo=timezone.utc)


def _real_settlement_rows() -> list[_FakeSettlement]:
    """The C7540 cash-settled assignment (-364 value, -369 net = $5 fee) plus
    the three worthless legs' zero-value Expiration removals."""
    return [
        _FakeSettlement(C7540, "Cash Settled Assignment", D("-364.0"), D("-369.0"), price=D("7540.0")),
        _FakeSettlement(P7535, "Expiration", D("0"), D("0")),
        _FakeSettlement(P7510, "Expiration", D("0"), D("0")),
        _FakeSettlement(C7565, "Expiration", D("0"), D("0")),
    ]


class _SettlementBroker:
    """Read-only day_settlements-only facade -- the exact surface
    `capture_settlements` may touch."""

    def __init__(self, rows) -> None:
        self._rows = rows

    async def day_settlements(self, day):
        return list(self._rows)


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


# --- Settlement cash is included or the day cannot confirm (v1.59) -----------

@given(parsers.parse("4 entry legs netting {credit} and a short C7540 with SPX settling "
                     "at {settle_price}"), target_fixture="settlement_vector_world")
def _(credit, settle_price):
    events = [CondorFilled(entry_id=f"{DAY}#1", net_credit=D("3.60"), fee=D("0.0488"),
                           legs=_real_condor_legs())]
    capture_result = asyncio.run(capture_settlements(
        events, _SettlementBroker(_real_settlement_rows()), DAY,
        now_iso=lambda: "2026-07-10T09:00:00-04:00"))
    return {"events": events, "capture_result": capture_result}


@then("the journaled settlement event records -369.00 from the broker's Receive-Deliver records")
def _(settlement_vector_world):
    events = settlement_vector_world["events"]
    cash = next(e for e in events if isinstance(e, SettlementRecorded) and e.symbol == C7540)
    assert cash.value == D("-369.0")
    assert cash.fee == D("5.0")
    assert cash.sub_type == "Cash Settled Assignment"
    assert cash.entry_id == f"{DAY}#1"  # attributed to the entry by its own leg book


@then("the day's true net is -13.88 and only then may it stamp broker-confirmed")
def _(settlement_vector_world):
    events = settlement_vector_world["events"]
    entry = entries_by_day(events)[DAY][0]
    assert entry_dollars(entry) == D("-13.88")  # +355.12 credit - 369.00 settlement

    # A broker facade reporting the FULL day cash (trades + settlement, EOD-01
    # v1.59's widened cash_and_fees) matches the bot's now-settled -13.88 --
    # ONLY THEN may the day stamp broker-confirmed.
    # NOTE: ReportReconciler compares str(Decimal) -- scale-sensitive, not
    # just value-equal (pre-existing, not introduced here). The bot's own
    # arithmetic lands on 4 decimal places (fee 0.0488 has scale 4), so the
    # "matching" broker figures are expressed at that SAME scale to actually
    # exercise a true match rather than a spurious string mismatch.
    full_broker = _MatchingBroker(positions=(), fills=[object()],
                                  cash_delta=D("-13.8800"), fees=D("9.8800"))
    reconciler = ReportReconciler(broker=full_broker, events=list(events),
                                  now=lambda: "2026-07-09T16:20:00-04:00")
    outcome = asyncio.run(reconciler.reconcile_day(DAY))
    assert outcome.status == "confirmed"


@then("a reconciler reading trade transactions only MUST fail this scenario")
def _(settlement_vector_world):
    """The decisive test: a reconciler whose broker facade reports ONLY the
    Trade-side cash (the old, pre-v1.59 shape -- +355.12, the settlement
    never folded in) must NOT confirm against the bot's now-settled -13.88.
    Proves the old trades-only reconciler behavior is dead."""
    events = settlement_vector_world["events"]
    trades_only_broker = _MatchingBroker(positions=(), fills=[object()],
                                         cash_delta=D("355.12"), fees=D("4.88"))
    events_copy = list(events)
    reconciler = ReportReconciler(broker=trades_only_broker, events=events_copy,
                                  now=lambda: "2026-07-09T16:20:00-04:00")
    outcome = asyncio.run(reconciler.reconcile_day(DAY))
    assert outcome.status != "confirmed"
    assert not any(isinstance(e, DayBrokerConfirmed) for e in events_copy)
    assert any(c.field == "cash_delta" for c in outcome.corrections)


# --- Settlement journaling is idempotent and never guesses -------------------

@given("the settlement backfill runs three times", target_fixture="idempotent_settlement_world")
def _():
    events = [CondorFilled(entry_id=f"{DAY}#1", net_credit=D("3.60"), fee=D("0.0488"),
                           legs=_real_condor_legs())]
    # OWN-03: C7540 carries a standing FOREIGN quarantine -- its settlement
    # cash is genuinely unattributable and must be withheld, never guessed.
    events.append(ForeignDetected(symbol=C7540))
    broker = _SettlementBroker(_real_settlement_rows())
    results = [asyncio.run(capture_settlements(
        events, broker, DAY, now_iso=lambda: "2026-07-10T09:00:00-04:00")) for _ in range(3)]
    return {"events": events, "results": results}


@then("settlement records exist exactly once per attributable expiring symbol")
def _(idempotent_settlement_world):
    events = idempotent_settlement_world["events"]
    recorded = [e for e in events if isinstance(e, SettlementRecorded)]
    symbols = [e.symbol for e in recorded]
    assert len(symbols) == len(set(symbols))       # never duplicated across the 3 runs
    assert set(symbols) == {P7535, P7510, C7565}   # C7540 withheld -- see next assertion
    results = idempotent_settlement_world["results"]
    assert results[0]["captured"] == 3             # first run captures the 3 unambiguous rows
    assert results[1]["captured"] == 0 and results[2]["captured"] == 0  # true no-op afterward


@then(parsers.parse('an OWN-03-ambiguous symbol is withheld with reason "{reason}"'))
def _(idempotent_settlement_world, reason):
    assert reason == "ambiguous_settlement"
    events = idempotent_settlement_world["events"]
    assert not any(isinstance(e, SettlementRecorded) and e.symbol == C7540 for e in events)
    results = idempotent_settlement_world["results"]
    # Never guessed: EVERY run re-detects the ambiguity, never journaling it once
    # and then forgetting -- there is nothing to "already resolve".
    assert all(r["ambiguous_settlements"] == 1 for r in results)
