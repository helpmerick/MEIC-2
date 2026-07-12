"""OWN-01/OWN-03 (2026-07-11 incident fix, REAL 2026-07-10 broker vector):
RPT-15's `ReportReconciler.reconcile_day` used to sum the WHOLE-ACCOUNT
`cash_and_fees(day)` against the bot's own numbers -- fatal on a shared
account (single-account operation is first-class, v1.49). On 2026-07-10 the
account held the bot's own condor (entry order 482621396: cash +43.68, fees
6.32) PLUS the operator's OWN separate trades: a 7580 condor (order
482759560, whose settlement was -466.00) and a Micro-Nasdaq futures put
(order 482542569, net -928.26). The whole-account sum bore no relation to
the bot's real +43.68 and spawned false `CorrectionRecord`s.

`reconcile_day` now scopes `cash_delta`/`fees`/`fill_count` to fills whose
order id is one the bot itself journaled (`reporting/own_orders.py`), plus
settlement rows whose symbol belongs unambiguously to one of those own
fills -- see application/report_reconciler.py's module docstring.

Every broker row below is modeled the way the REAL tastytrade rows come
back (operator-verified 2026-07-10): a signed `value`, a signed `net_value`,
and the row's fee as exactly `value - net_value` (`net_value = value + fees`,
fees negative) -- e.g. the real recovery sale (value 10.00, net 9.28 -> 0.72
cost) and the real settlement (value -461.00, net -466.00 -> 5.00 cost).
Values are scale-2, as the broker actually returns them -- NOT re-scaled to
flatter the comparison (see `_agrees` in report_reconciler.py: the bot's own
fold lands on scale-4 "6.3200" for the identical $6.32, and the reconciler
compares NUMBERS, not Decimal string forms).
"""
import asyncio
import types
from decimal import Decimal as D

from meic.application.report_reconciler import ReportReconciler
from meic.domain.events import (
    CondorFilled,
    CorrectionRecord,
    DayArmed,
    DayBrokerConfirmed,
    EntryClosed,
    EntrySkipped,
    StopPlaced,
)

DAY = "2026-07-10"
BOT_ORDER_ID = "482621396"                # the bot's own condor entry order
BOT_STOP_ORDER_ID = "482621556"           # the bot's own resting stop (StopPlaced, v1.60)
OPERATOR_CONDOR_ORDER_ID = "482759560"    # the operator's own 7580 condor
OPERATOR_MNQ_ORDER_ID = "482542569"       # the operator's own Micro-Nasdaq futures put

P_SHORT = "SPXW  260710P07600000"
P_LONG = "SPXW  260710P07575000"
C_SHORT = "SPXW  260710C07650000"
C_LONG = "SPXW  260710C07675000"
OPERATOR_MNQ_SYMBOL = "./MNQU26P20500"
OPERATOR_CONDOR_SYMBOL = "SPXW  260710P07580000"  # the operator's OWN 7580 condor leg

# The bot's TRUE numbers for 2026-07-10 -- the vector this whole fix exists for.
TRUE_CASH_DELTA = D("43.68")
TRUE_FEES = D("6.32")


def _row(*, order_id=None, symbol, value, net_value, **extra):
    """One broker Transaction row, real shape: signed `value`, signed
    `net_value`; the fee is the difference (never a separate field the
    reconciler has to enumerate). Receive-Deliver settlement rows carry NO
    order id at all -- hence `order_id=None` by default."""
    return types.SimpleNamespace(order_id=order_id, symbol=symbol, value=value,
                                 net_value=net_value, **extra)


def _own_fills():
    """The bot's OWN 6 fills for 2026-07-10 (operator-verified totals):
    4 entry legs (fee 1.22 each = 4.88) + the stop's buy-to-close and the
    LEX long-sale recovery (fee 0.72 each = 1.44). Fees total EXACTLY 6.32;
    net_values total EXACTLY +43.68."""
    return [
        # --- the entry condor (order 482621396): gross value +50.00, fees 4.88
        _row(order_id=BOT_ORDER_ID, symbol=P_SHORT, value=D("300.00"), net_value=D("298.78")),
        _row(order_id=BOT_ORDER_ID, symbol=P_LONG, value=D("-180.00"), net_value=D("-181.22")),
        _row(order_id=BOT_ORDER_ID, symbol=C_SHORT, value=D("280.00"), net_value=D("278.78")),
        _row(order_id=BOT_ORDER_ID, symbol=C_LONG, value=D("-350.00"), net_value=D("-351.22")),
        # --- the stop's buy-to-close + the LEX recovery sale (order 482621556):
        # the REAL rows the operator pulled -- value 10.00/net 9.28 is verbatim.
        _row(order_id=BOT_STOP_ORDER_ID, symbol=C_SHORT, value=D("-10.00"), net_value=D("-10.72")),
        _row(order_id=BOT_STOP_ORDER_ID, symbol=C_LONG, value=D("10.00"), net_value=D("9.28")),
    ]


def _foreign_mnq_fill():
    """The operator's OWN Micro-Nasdaq futures put -- a different order id, on
    the SAME shared account, that must never enter the bot's ledger (OWN-01:
    "operator/manual trades never enter the ledger, its P&L, or its risk
    marks"). Its -928.26 is the single biggest thing the old whole-account
    sum wrongly swept in."""
    return _row(order_id=OPERATOR_MNQ_ORDER_ID, symbol=OPERATOR_MNQ_SYMBOL,
                value=D("-926.00"), net_value=D("-928.26"))


def _foreign_condor_settlement():
    """The operator's OWN 7580 condor settlement -- a symbol the bot never
    traded, and (like every Receive-Deliver row) no order id at all. Real
    values: value -461.00, net_value -466.00 -> a 5.00 fee."""
    return _row(symbol=OPERATOR_CONDOR_SYMBOL, value=D("-461.00"), net_value=D("-466.00"),
                transaction_sub_type="Assignment", price=D("7580.0"), quantity=D("1"))


class _SharedAccountBroker:
    """A read-only facade over a SHARED account: `day_fills`/`day_settlements`
    return the bot's own rows interleaved with the operator's foreign ones --
    exactly what a real tastytrade account-history GET returns (it has no
    concept of "whose" a transaction is beyond order id / symbol).
    `cash_and_fees` still exists on the facade but returns the OLD,
    whole-account aggregate this fix stops using entirely: if `reconcile_day`
    ever reads it again, the regression test below fails."""

    def __init__(self, fills, settlements):
        self._fills, self._settlements = fills, settlements

    async def positions(self):
        return []  # the bot believes it is flat; broker agrees (0 positions)

    async def day_fills(self, day):
        return list(self._fills)

    async def day_settlements(self, day):
        return list(self._settlements)

    async def cash_and_fees(self, day):
        # The poisoned whole-account figure: bot's +43.68, minus the operator's
        # -928.26 MNQ put and -466.00 condor settlement.
        return D("-1350.58"), D("500.00")


def _bot_log():
    """The bot's own journal for the day: one filled condor (net_credit 0.50,
    fee 0.0632/share) with its resting stop, closed by EOD. Folds to net
    (0.50 - 0.0632) * 100 = $43.68 and fees 0.0632 * 100 = $6.32 -- the same
    TRUE numbers the bot's own broker rows above independently sum to.

    The `StopPlaced.broker_order_id` (v1.60) matters: it is what makes the
    stop's two broker rows the BOT'S own (they belong in the cash/fee sums)
    rather than a stranger's. Without it they would be misread as the
    operator's -- the mirror image of the bug this module fixes."""
    return [
        CondorFilled(entry_id=f"{DAY}#1", net_credit=D("0.50"), fee=D("0.0632"),
                     broker_order_id=BOT_ORDER_ID),
        StopPlaced(entry_id=f"{DAY}#1", side="CALL", trigger=D("2.80"),
                   broker_order_id=BOT_STOP_ORDER_ID),
        EntryClosed(entry_id=f"{DAY}#1", initiator="eod"),
    ]


def test_the_bots_own_rows_sum_to_the_true_cash_and_fees():
    """Pins the vector itself, independent of the reconciler: the 6 own fills
    net to +43.68, and their `value - net_value` fees total exactly 6.32."""
    fills = _own_fills()
    assert sum((f.net_value for f in fills), D("0")) == TRUE_CASH_DELTA
    assert sum((f.value - f.net_value for f in fills), D("0")) == TRUE_FEES


def test_shared_account_reconcile_ignores_the_operators_foreign_rows():
    """THE REGRESSION TEST for the 2026-07-10 incident: a day whose broker read
    returns the bot's own 6 fills PLUS a foreign fill PLUS a foreign settlement
    must confirm cleanly on the bot's real +43.68 / 6.32 -- the operator's
    -928.26 and -466.00 must never be summed in. Before the OWN-01/OWN-03 fix
    (whole-account `cash_and_fees`), the reconciler saw -1350.58 against the
    bot's +43.68 and spawned false CorrectionRecords."""
    events = _bot_log()
    broker = _SharedAccountBroker(
        fills=_own_fills() + [_foreign_mnq_fill()],
        settlements=[_foreign_condor_settlement()])
    reconciler = ReportReconciler(broker=broker, events=events,
                                  now=lambda: "2026-07-10T16:20:00-04:00")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    assert outcome.status == "confirmed", (
        f"foreign rows leaked into the reconcile: {outcome.corrections}")
    assert not any(isinstance(e, CorrectionRecord) for e in events)

    # The day is stamped with the bot's OWN numbers -- and they are the true ones.
    stamp = next(e for e in events if isinstance(e, DayBrokerConfirmed))
    assert D(stamp.checked["cash_delta"]) == TRUE_CASH_DELTA   # +43.68, never -1350.58
    assert D(stamp.checked["fees"]) == TRUE_FEES               # 6.32, never the account's
    assert stamp.checked["fill_count"] == "1"                  # the bot's entry, not the account's

    # The foreign settlement's symbol never belonged to an own fill, so it is
    # simply "not ours" -- not even counted ambiguous.
    assert outcome.ambiguous_settlements == 0


def test_a_foreign_fill_alone_never_moves_the_bots_numbers():
    """Isolates the single worst row: with ONLY the operator's -928.26 MNQ put
    added to the bot's own day, the reconcile is byte-identical to a day
    without it. OWN-01: the operator's trades touch nothing of the bot's."""
    clean_events, shared_events = _bot_log(), _bot_log()

    clean = ReportReconciler(broker=_SharedAccountBroker(fills=_own_fills(), settlements=[]),
                             events=clean_events, now=lambda: "t")
    shared = ReportReconciler(
        broker=_SharedAccountBroker(fills=_own_fills() + [_foreign_mnq_fill()], settlements=[]),
        events=shared_events, now=lambda: "t")

    assert asyncio.run(clean.reconcile_day(DAY)).status == "confirmed"
    assert asyncio.run(shared.reconcile_day(DAY)).status == "confirmed"
    clean_stamp = next(e for e in clean_events if isinstance(e, DayBrokerConfirmed))
    shared_stamp = next(e for e in shared_events if isinstance(e, DayBrokerConfirmed))
    assert clean_stamp.checked == shared_stamp.checked


def test_fill_count_compares_entries_to_entries_not_entries_to_leg_rows():
    """`day_snapshot.fill_count` counts FILLED ENTRIES (1 per condor); the
    broker returns one row PER LEG (6 own rows on this real day). Counting
    broker ROWS would disagree with the bot on every real day -- and since
    `reporting/corrections.py` RENDERS a corrected field's broker value, the
    dashboard would then show "6 fills" for a one-condor day. The broker-side
    equivalent is the number of DISTINCT ENTRY orders that produced fills: the
    stop's own rows are the bot's (they count in cash/fees) but are NOT
    entries."""
    events = _bot_log()
    broker = _SharedAccountBroker(fills=_own_fills(), settlements=[])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "t")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    assert outcome.status == "confirmed", (
        f"fill_count compared rows to entries: {outcome.corrections}")
    stamp = next(e for e in events if isinstance(e, DayBrokerConfirmed))
    assert stamp.checked["fill_count"] == "1"  # one condor -- NOT the 6 broker leg rows


def test_a_real_disagreement_still_corrects():
    """The scoping must not become a way to never disagree: a broker row whose
    cash genuinely differs from the bot's own figure still raises its
    CorrectionRecord (RPT-15's zero-drift rule is intact -- nothing here is
    an epsilon or a rounding tolerance)."""
    events = _bot_log()
    wrong = _own_fills()
    wrong[0] = _row(order_id=BOT_ORDER_ID, symbol=P_SHORT,
                    value=D("301.00"), net_value=D("299.78"))  # a real +1.00 disagreement
    broker = _SharedAccountBroker(fills=wrong, settlements=[])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "t")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    assert outcome.status == "corrected"
    cash = next(c for c in outcome.corrections if c.field == "cash_delta")
    assert D(cash.bot_value) == TRUE_CASH_DELTA and D(cash.broker_value) == D("44.68")
    assert D(cash.diff) == D("1.00")


# --- The unattributable day: refuse to invent the broker's side --------------

class _FakeAlerts:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def alert(self, level, message, **context) -> None:
        self.records.append((level, message))

    @property
    def critical(self) -> list[tuple[str, str]]:
        return [r for r in self.records if r[0] == "critical"]


def _legacy_bot_log():
    """A day placed by PRE-FIX code: `CondorFilled` carries NO
    `broker_order_id` (the field did not exist), and neither does its stop.
    This is the REAL 2026-07-10 journal shape -- not one event for that entry
    names any broker order id."""
    return [
        CondorFilled(entry_id=f"{DAY}#1", net_credit=D("0.50"), fee=D("0.0632")),
        StopPlaced(entry_id=f"{DAY}#1", side="CALL", trigger=D("2.80")),
        EntryClosed(entry_id=f"{DAY}#1", initiator="eod"),
    ]


def test_a_day_whose_entries_journaled_no_order_id_is_refused_never_fabricated():
    """THE FABRICATION GUARD. With no journaled entry order id, EVERY broker
    row scopes out as foreign, so the scoped sums come to cash 0 / fees 0.
    Journaling those as broker truth would assert THE BROKER SAYS ZERO -- it
    says no such thing; we just cannot tell which rows are ours. And
    `reporting/corrections.py` RENDERS a corrected field's broker value, so
    the dashboard would show a confident, fictional "$0".

    The day must be refused outright: nothing appended, nothing stamped, one
    critical alert pointing at RPT-16's operator-supplied order-id path."""
    events = _legacy_bot_log()
    before = list(events)
    alerts = _FakeAlerts()
    broker = _SharedAccountBroker(fills=_own_fills() + [_foreign_mnq_fill()],
                                  settlements=[_foreign_condor_settlement()])
    reconciler = ReportReconciler(broker=broker, events=events, alerts=alerts, now=lambda: "t")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    assert outcome.status == "unattributable"
    assert events == before, "an unattributable day must append NOTHING"
    assert not any(isinstance(e, CorrectionRecord) for e in events)   # no fabricated 0s
    assert not any(isinstance(e, DayBrokerConfirmed) for e in events)  # never stamped either
    assert len(alerts.critical) == 1
    assert "cannot be auto-reconciled" in alerts.critical[0][1]


def test_the_unattributable_alert_fires_once_per_day_not_once_per_health_tick():
    """An unattributable day stamps nothing, so `_maybe_eod_reconcile_once`'s
    "already stamped?" gate keeps RETRYING it every health tick -- correct (it
    may yet become reconcilable), but the critical alert must not re-fire once
    a minute all evening."""
    events = _legacy_bot_log()
    alerts = _FakeAlerts()
    broker = _SharedAccountBroker(fills=_own_fills(), settlements=[])
    reconciler = ReportReconciler(broker=broker, events=events, alerts=alerts, now=lambda: "t")

    for _ in range(5):  # five health ticks
        assert asyncio.run(reconciler.reconcile_day(DAY)).status == "unattributable"

    assert len(alerts.critical) == 1  # alerted once, not five times
    assert events == _legacy_bot_log()  # still nothing appended, every tick


def test_an_unattributable_day_never_crashes_without_an_alerts_sink():
    """`alerts` is optional on this service (every pre-v1.59 caller omits it)."""
    events = _legacy_bot_log()
    reconciler = ReportReconciler(broker=_SharedAccountBroker(fills=_own_fills(), settlements=[]),
                                  events=events, now=lambda: "t")
    assert asyncio.run(reconciler.reconcile_day(DAY)).status == "unattributable"


def test_a_day_with_no_entries_at_all_still_reconciles_normally():
    """The guard must NOT swallow a legitimately empty day. Nothing was filled,
    so there is nothing to attribute -- a genuine flat/no-activity day still
    reconciles exactly as before (bot 0/0/flat vs a broker showing no rows of
    ours)."""
    events: list = [DayArmed(date=DAY, entry_count=1),
                    EntrySkipped(date=DAY, entry_number=1, reason="unfilled_at_floor")]
    alerts = _FakeAlerts()
    # The account is NOT empty -- it holds the operator's own trades. They must
    # still scope out, leaving the bot's honest 0/0.
    broker = _SharedAccountBroker(fills=[_foreign_mnq_fill()],
                                  settlements=[_foreign_condor_settlement()])
    reconciler = ReportReconciler(broker=broker, events=events, alerts=alerts, now=lambda: "t")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    assert outcome.status == "confirmed"
    assert not alerts.critical
    stamp = next(e for e in events if isinstance(e, DayBrokerConfirmed))
    assert stamp.checked["fill_count"] == "0"


def test_a_partially_journaled_day_is_attributable_and_reconciles():
    """The guard is "NOT ONE entry carries an id", not "every entry does": a
    day with at least one journaled entry order id can be scoped, so it
    reconciles rather than being refused."""
    events = _bot_log()  # its CondorFilled DOES carry broker_order_id
    broker = _SharedAccountBroker(fills=_own_fills() + [_foreign_mnq_fill()], settlements=[])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "t")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    assert outcome.status == "confirmed"  # the 2026-07-10 vector still reconciles: 43.68 / 6.32


def test_a_symbol_shared_between_an_own_fill_and_a_foreign_fill_is_never_guessed():
    """OWN-03 shared-symbol guard: if the SAME symbol appears on both an own
    fill and a foreign (non-own) fill the same day, its settlement is genuinely
    ambiguous -- excluded from the cash/fee sum and counted, never guessed which
    side it belongs to (mirrors capture_settlements' identical guard)."""
    events = _bot_log()
    shared_symbol = C_SHORT  # the operator happened to trade one of our strikes too
    foreign_fill_same_symbol = _row(order_id=OPERATOR_CONDOR_ORDER_ID, symbol=shared_symbol,
                                    value=D("-100.00"), net_value=D("-101.00"))
    ambiguous_settlement = _row(symbol=shared_symbol, value=D("-45.00"), net_value=D("-50.00"),
                                transaction_sub_type="Expiration", quantity=D("1"))

    broker = _SharedAccountBroker(fills=_own_fills() + [foreign_fill_same_symbol],
                                  settlements=[ambiguous_settlement])
    reconciler = ReportReconciler(broker=broker, events=events, now=lambda: "t")

    outcome = asyncio.run(reconciler.reconcile_day(DAY))

    assert outcome.ambiguous_settlements == 1
    # The withheld row's -50.00 cash and 5.00 fee never entered the compare --
    # the bot's own rows still sum to the true +43.68 / 6.32, so the day
    # confirms rather than mis-correcting on a row nobody can attribute.
    assert outcome.status == "confirmed", (
        f"ambiguous settlement leaked into the compare: {outcome.corrections}")
    stamp = next(e for e in events if isinstance(e, DayBrokerConfirmed))
    assert D(stamp.checked["cash_delta"]) == TRUE_CASH_DELTA
    assert D(stamp.checked["fees"]) == TRUE_FEES
