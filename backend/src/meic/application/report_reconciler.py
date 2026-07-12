"""RPT-15 -- EOD broker reconcile-and-correct (operator rule: zero drift).

Structurally read-only: this module accepts a narrow, duck-typed broker
FACADE (`positions` / `day_fills` / `day_settlements` / `cash_and_fees`) --
never a BrokerGateway, and it imports NOTHING from `meic.adapters` or
`meic.composition`, so it is structurally incapable of placing, replacing,
or cancelling an order (mirrors the guarantee TC-RPT-06 proves for
`meic.reporting`; asserted directly for this module by
tests/application/test_report_reconciler_structural.py). The composition
root is responsible for handing it a facade that only forwards those read
calls to the real broker (see adapters/api/server.py's `_BrokerReadFacade`)
-- this module never touches `TastytradeAdapter` (or any adapter) at all.

OWN-01/OWN-03 (2026-07-11 incident fix): the account is shared with the
operator's own trading (single-account operation is first-class, v1.49), so
`day_fills`/`day_settlements` return EVERY transaction on the account, not
just the bot's. Before this fix, `cash_delta`/`fees` came straight from the
broker's whole-account `cash_and_fees(day)` -- summing the operator's own,
unrelated trades into "broker truth" and producing false mismatches (the
2026-07-10 incident: the account also held the operator's own condor and a
futures put; the whole-account sum computed a garbage cash_delta of -534.46
against the bot's real +43.68). `cash_delta`/`fees`/`fill_count` are now
computed ONLY from fills whose `order_id` is one the bot itself journaled
placing (`reporting/own_orders.own_order_ids`, read off `CondorFilled`/
`StopPlaced`/`DecayBuybackPlaced`/`LexOrderPlaced`), plus settlement rows
whose symbol belongs unambiguously to one of those own fills. A symbol seen
on BOTH an own fill and a foreign (non-own) fill the same day is genuinely
ambiguous -- its settlement is excluded and counted, never guessed (mirrors
`application/settlement_capture.py`'s identical OWN-03 guard). `positions()`
(the whole-account `flat` check) is UNCHANGED -- RPT-15 has never scoped
that call, and this fix does not expand its scope.

Match -> `DayBrokerConfirmed`. Mismatch -> one `CorrectionRecord` per
diverging field (never a silent overwrite) plus a critical alert. Broker
unreachable -> nothing is appended; the day stays bot-computed and is
retried at the next boot/tick (never auto-confirmed).

UNATTRIBUTABLE -> also nothing is appended. A day whose entries journaled no
broker order id (every entry placed by pre-fix code) cannot have ANY broker
row scoped to the bot, so the honest scoped sums would be cash 0 / fees 0 --
and journaling those as "broker truth" would be a FABRICATION the dashboard
then renders as a confident "$0". Such a day is refused and alerted instead,
for reconciliation through RPT-16's operator-supplied order-id path. Same
principle as the OWN-03 ambiguous-symbol guard: when attribution is
impossible, withhold -- never guess. (See `reconcile_day`.)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

from meic.domain.events import CondorFilled, CorrectionRecord, DayBrokerConfirmed, OwnOrderIdBackfilled
from meic.reporting.folds import day_snapshot, entry_day
from meic.reporting.own_orders import own_order_ids


class ReadOnlyBrokerFacade(Protocol):
    """The ONLY broker surface RPT-15 may touch -- four read fetches. No
    submit/replace/cancel method exists on this type at all."""

    async def positions(self) -> list[Any]: ...
    async def day_fills(self, day: str) -> list[Any]: ...
    async def day_settlements(self, day: str) -> list[Any]: ...
    async def cash_and_fees(self, day: str) -> tuple[Decimal, Decimal]: ...


# -- broker Transaction field readers (OWN-01/OWN-03 scoping) -----------------
# Same defensive `getattr`/`Decimal(str(...))` shape as
# `application/backfill.py` and `application/settlement_capture.py`, which
# read the identical tastytrade SDK Trade/Receive-Deliver rows -- missing or
# None numerics are honest zeros here (this is a sum, not a per-row record),
# never fabricated non-zero values.


def _order_id_of(t: Any) -> str | None:
    v = getattr(t, "order_id", None)
    return None if v is None else str(v)


def _symbol_of(t: Any) -> str:
    v = getattr(t, "symbol", None)
    return "" if v is None else str(v)


def _net_value_of(t: Any) -> Decimal:
    v = getattr(t, "net_value", None)
    return Decimal("0") if v is None else Decimal(str(v))


def _fee_cost_of(t: Any) -> Decimal:
    """This row's total fee, as a POSITIVE cost (the bot's own `fee` fields
    are positive costs -- PNL-01 convention), DERIVED as `value - net_value`
    rather than summed from the broker's individual fee-category fields.

    Why the derivation and not the components (operator-verified against the
    real 2026-07-10 broker rows):
      - It is definitionally EXACT. The broker's own invariant is
        `net_value = value + fees` with fees signed negative, so
        `value - net_value` IS the fee, to the cent. Verified on every real
        row: a fill (value 10.00, net 9.28) -> 0.72; the mirror-side fill
        (value -480.00, net -480.72) -> 0.72; the settlement (value -461.00,
        net -466.00) -> 5.00. Summed over the day's 6 own condor fills it
        lands on exactly 6.32, the true fee.
      - It CANNOT silently undercount. A component sum only covers the fee
        categories we happened to enumerate; the day tastytrade adds a new
        one, that sum quietly goes light and RPT-15 would confirm a wrong
        number. The derivation has no category list to fall behind.
      - It matches `application/backfill.py`, which already derives a
        settlement row's fee the same way (see its docstring) -- one
        convention across the codebase, not two.

    A row missing either field contributes 0 (honest absence -- there is
    nothing to derive from, and fabricating a fee would poison the very
    figure RPT-15 exists to check)."""
    value = getattr(t, "value", None)
    net_value = getattr(t, "net_value", None)
    if value is None or net_value is None:
        return Decimal("0")
    return Decimal(str(value)) - Decimal(str(net_value))


@dataclass(frozen=True)
class ReconcileOutcome:
    day: str
    # "confirmed"      -- bot numbers matched broker truth; DayBrokerConfirmed stamped
    # "corrected"      -- a field diverged; one CorrectionRecord per field
    # "unreachable"    -- broker fetch failed; NOTHING appended, retried next tick
    # "unattributable" -- the day's entries carry no journaled broker order id, so
    #                     none of the broker's rows can be scoped to the bot;
    #                     NOTHING appended (see `reconcile_day`'s guard)
    status: str
    corrections: tuple[CorrectionRecord, ...] = ()
    # OWN-03: settlement rows withheld this reconcile because their symbol was
    # claimed by BOTH an own fill and a foreign fill the same day -- genuinely
    # unattributable from broker data alone, never guessed (mirrors
    # `capture_settlements`'s `ambiguous_settlements` result key).
    ambiguous_settlements: int = 0


def _diff(bot_value: str, broker_value: str) -> str:
    try:
        return str(Decimal(broker_value) - Decimal(bot_value))
    except (InvalidOperation, ValueError):
        return "n/a"  # e.g. the "flat" bool check -- no numeric diff to report


def _agrees(bot_value: str, broker_value: str) -> bool:
    """Do the bot's and the broker's figures for one field AGREE?

    Numeric fields are compared as NUMBERS, not as strings. `Decimal` string
    form carries SCALE, and the two sides legitimately arrive at different
    scales for the identical amount: the bot's fold multiplies a per-share
    fee (0.0632, scale 4) by the contract multiplier and lands on "6.3200",
    while the broker's own rows are scale-2 and sum to "6.32". Those are the
    SAME $6.32. A string compare calls them different and emits a
    `CorrectionRecord` whose own `diff` is 0.0000 -- a correction that
    corrects nothing, on a day that actually reconciled perfectly. Pinned by
    the real 2026-07-10 vector (see
    tests/application/test_report_reconciler_own_scoping.py): bot
    43.6800/6.3200 vs broker 43.68/6.32 -- a clean day that the string
    compare would have flagged twice.

    Non-numeric fields (the `flat` bool check, "True"/"False") fall back to
    the exact string compare, which is the right test for them.

    RPT-15's zero-drift rule is about VALUES drifting, never about their
    formatting: nothing here loosens the comparison (no epsilon, no
    rounding) -- an actual one-cent disagreement still corrects, exactly as
    before."""
    try:
        return Decimal(bot_value) == Decimal(broker_value)
    except (InvalidOperation, ValueError):
        return bot_value == broker_value


class ReportReconciler:
    """RPT-15: compare one day's bot-computed numbers against broker truth
    and append the outcome to the SAME durable event log every other service
    appends to (`events`; typically `composition.events`, a `DurableEventLog`
    -- see application/event_log.py -- so `CorrectionRecord`/
    `DayBrokerConfirmed` are journaled exactly like any other domain event)."""

    def __init__(self, *, broker: ReadOnlyBrokerFacade, events: list, alerts: Any = None,
                 now: Callable[[], str] | None = None) -> None:
        self._broker = broker
        self._events = events
        self._alerts = alerts
        self._now = now or (lambda: datetime.now().astimezone().isoformat())
        # An unattributable day journals NOTHING, so the health tick's
        # "already stamped?" gate (server.py `_maybe_eod_reconcile_once`) does
        # not see it and correctly RETRIES it every tick -- which is what we
        # want for a day that might yet become reconcilable. But the retry must
        # not re-raise its critical alert once a minute all evening: alert ONCE
        # per day per process. (Deliberately in-memory, not journaled: a
        # restart re-alerting once is the right behaviour -- the operator's
        # attention is being asked for, and a fresh process has no reason to
        # assume the ask was already seen.)
        self._unattributable_alerted: set[str] = set()

    async def reconcile_day(self, day: str) -> ReconcileOutcome:
        try:
            positions = await self._broker.positions()
            fills = await self._broker.day_fills(day)
            settlements = await self._broker.day_settlements(day)
        except Exception:  # noqa: BLE001 -- ANY failure is "unreachable", never a crash
            return ReconcileOutcome(day=day, status="unreachable")

        # OWN-01/OWN-03: scope to the bot's OWN transactions only -- never the
        # whole shared account (see module docstring for the incident this fixes).
        own = own_order_ids(self._events)

        bot = day_snapshot(self._events, day)

        # THE UNATTRIBUTABLE-DAY GUARD. Scoping is only possible if the day's
        # entries actually journaled a broker order id. Entries placed by
        # pre-fix code did not (`CondorFilled.broker_order_id` did not exist --
        # the real 2026-07-10 day is exactly this: not one event for that
        # entry carries any order id). For such a day `own` names nothing of
        # ours, so EVERY broker row scopes out, and the sums below would come
        # to cash_delta = 0 / fees = 0 -- and we would then journal
        # `CorrectionRecord`s asserting the BROKER SAYS ZERO. It says no such
        # thing: we simply cannot tell which of its rows are ours. And because
        # `reporting/corrections.py` RENDERS a corrected field's broker value,
        # the dashboard would show a confident "$0" that is pure fiction.
        #
        # Reporting NOTHING is strictly better than reporting an invented
        # number: RPT-15's zero-drift rule must never be satisfied by making
        # up the broker's side. So this day is refused -- no CorrectionRecord,
        # no DayBrokerConfirmed, not stamped either way -- and the operator is
        # alerted to reconcile it through RPT-16's existing operator-supplied
        # order-id path (application/backfill.py), which is precisely the
        # escape hatch for days that predate the journal. Same philosophy as
        # the OWN-03 ambiguous-symbol guard below: when attribution is
        # genuinely impossible, withhold -- never guess.
        #
        # A day on which the bot filled NOTHING (`fill_count == 0`) is NOT
        # unattributable -- there is nothing to attribute, and a genuine
        # flat/no-activity day must still reconcile normally, exactly as before.
        #
        # OWN-03 / RPT-16 escape hatch (2026-07-12): a day whose `CondorFilled`
        # predates order-id journaling can be made attributable AFTER THE FACT
        # by an operator-supplied `OwnOrderIdBackfilled(role="entry")` for one
        # of that day's entries (application/backfill_order_ids.py) -- the
        # real 2026-07-10 incident this escape hatch exists for. Such a day
        # must count as attributable too, not just a day whose `CondorFilled`
        # itself carried the id at fill time.
        day_entries = [e for e in self._events
                       if isinstance(e, CondorFilled) and entry_day(e.entry_id) == day]
        backfilled_entry_ids_today = {
            e.broker_order_id for e in self._events
            if isinstance(e, OwnOrderIdBackfilled) and e.role == "entry"
            and entry_day(e.entry_id) == day}
        if (bot.fill_count > 0 and not any(e.broker_order_id for e in day_entries)
                and not backfilled_entry_ids_today):
            if self._alerts is not None and day not in self._unattributable_alerted:
                self._unattributable_alerted.add(day)  # once per day -- see __init__
                self._alerts.alert(
                    "critical",
                    f"RPT-15: {day} cannot be auto-reconciled -- no entry order ids were "
                    f"journaled for that day, so the broker's rows cannot be attributed to "
                    f"the bot (OWN-01/OWN-03). Reconcile it via the operator-supplied "
                    f"order-id path (RPT-16 backfill); the day is left unstamped.",
                    entries=str(len(day_entries)))
            return ReconcileOutcome(day=day, status="unattributable")

        own_fills = [f for f in fills if _order_id_of(f) in own]
        own_symbols = {_symbol_of(f) for f in own_fills}
        foreign_symbols = {_symbol_of(f) for f in fills if _order_id_of(f) not in own}
        ambiguous_symbols = own_symbols & foreign_symbols

        own_settlements: list[Any] = []
        ambiguous_settlements = 0
        for s in settlements:
            symbol = _symbol_of(s)
            if symbol not in own_symbols:
                continue  # not one of the bot's own symbols -- not ours
            if symbol in ambiguous_symbols:
                # OWN-03: claimed by BOTH an own fill and a foreign fill today
                # -- genuinely unattributable, never guessed.
                ambiguous_settlements += 1
                continue
            own_settlements.append(s)

        own_rows = own_fills + own_settlements
        cash_delta = sum((_net_value_of(t) for t in own_rows), Decimal("0"))
        # `value - net_value` per row, already a POSITIVE cost -- see
        # `_fee_cost_of` for why this derivation beats summing the broker's
        # individual fee-category fields.
        fees = sum((_fee_cost_of(t) for t in own_rows), Decimal("0"))

        # `fill_count` compares LIKE FOR LIKE. `day_snapshot.fill_count`
        # (reporting/folds.py) counts FILLED ENTRIES -- one per condor -- while
        # the broker returns one row PER LEG (4 for an entry, more once the
        # stop's buy-to-close and the LEX recovery sale land: the real
        # 2026-07-10 day is 1 entry but 6 own rows). Counting rows here would
        # therefore disagree with the bot on EVERY real day, and because
        # `reporting/corrections.py` RENDERS the broker value of a corrected
        # field, the dashboard would show "6 fills" for a one-condor day. The
        # broker-side equivalent of "entries filled" is the number of DISTINCT
        # ENTRY orders (a journaled `CondorFilled.broker_order_id`) that
        # actually produced fills today -- stop/LEX/decay orders are the bot's
        # own (they belong in the cash and fee sums above) but they are not
        # entries and must not inflate this count.
        # OWN-03 / RPT-16 (2026-07-12): a backfilled `role="entry"` id is an
        # entry order id too, exactly like one journaled directly on
        # `CondorFilled` at fill time -- see the unattributable guard above.
        entry_order_ids = {str(e.broker_order_id) for e in self._events
                           if isinstance(e, CondorFilled) and e.broker_order_id is not None}
        entry_order_ids |= {str(e.broker_order_id) for e in self._events
                            if isinstance(e, OwnOrderIdBackfilled) and e.role == "entry"}
        broker_entry_fills = {oid for f in own_fills
                              if (oid := _order_id_of(f)) in entry_order_ids}

        checks = {  # `bot` was folded above, before the unattributable guard
            "flat": (str(bot.flat), str(len(positions) == 0)),
            "fill_count": (str(bot.fill_count), str(len(broker_entry_fills))),
            "cash_delta": (str(bot.net), str(cash_delta)),
            "fees": (str(bot.fees), str(fees)),
        }
        at = self._now()
        corrections: list[CorrectionRecord] = []
        for field_name, (bot_v, broker_v) in checks.items():
            if not _agrees(bot_v, broker_v):
                rec = CorrectionRecord(date=day, field=field_name, bot_value=bot_v,
                                       broker_value=broker_v, diff=_diff(bot_v, broker_v), at=at,
                                       # scope="own" (2026-07-12 fix): this record's broker_v
                                       # was computed from ONLY the bot's own journaled order
                                       # ids (own_order_ids/OWN-01/OWN-03 scoping above) -- it
                                       # is genuine broker truth, safe for corrected_value to
                                       # apply to the dashboard (see CorrectionRecord docstring).
                                       scope="own")
                self._events.append(rec)
                corrections.append(rec)

        if corrections:
            if self._alerts is not None:
                self._alerts.alert(
                    "critical",
                    f"RPT-15: {day} broker reconciliation found {len(corrections)} correction(s)",
                    fields=",".join(c.field for c in corrections))
            return ReconcileOutcome(day=day, status="corrected", corrections=tuple(corrections),
                                    ambiguous_settlements=ambiguous_settlements)

        self._events.append(DayBrokerConfirmed(
            date=day, at=at, checked={k: v[0] for k, v in checks.items()}))
        return ReconcileOutcome(day=day, status="confirmed",
                                ambiguous_settlements=ambiguous_settlements)
