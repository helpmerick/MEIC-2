"""RPT-16 (proposed amendment — AMENDMENT-PROPOSAL-historical-backfill.md):
the one-time, operator-triggered import of pre-journal broker history into
the durable event log. Imported history enters as `ExternalFillImported`
events, one per broker fill leg -- NEVER as a synthetic `CondorFilled`: the
log is authoritative for INTENT (REC-02), and an imported row carries no
recorded intent, only the broker's own record of what happened.

Structurally read-only at the broker (RPT-16 rule 6): `broker_reads` is a
narrow, duck-typed facade exposing only `day_fills(day)` / `day_settlements(day)`
-- mirrors `ReadOnlyBrokerFacade` in application/report_reconciler.py, never a
real order-capable adapter. This module imports nothing from `meic.adapters`
or `meic.composition` and references no submit/replace/cancel capability
anywhere (see tests/application/test_backfill_structural.py, which asserts
this the same way tests/application/test_report_reconciler_structural.py
does for the reconciler).

Broker `Transaction` field mapping (tastytrade SDK, `tastytrade/account.py`,
verified against the installed .venv copy) that this module reads:
  - `order_id: int | None`      -- OWN-03 match key (compared as `str`); None
    on a Receive-Deliver settlement row -- those match by SYMBOL instead.
  - `symbol: str | None`        -- the broker's own instrument symbol
  - `action: OrderAction | None` -- a StrEnum ("Sell to Open" | "Buy to Open" |
    "Sell to Close" | "Buy to Close" | ...); `str(action)` IS the value text
  - `transaction_type: str` / `transaction_sub_type: str` -- "Receive Deliver"
    / ("Cash Settled Assignment" | "Expiration" | "Assignment") for a
    settlement row; `str(transaction_sub_type)` becomes the imported event's
    `action` for these rows (there is no OrderAction to report)
  - `quantity: Decimal | None`
  - `price: Decimal | None`     -- broker-allocated fill price (Trade rows);
    the settle strike reference on a settlement row, or None
  - `value: Decimal`            -- ALWAYS present; the transaction's own
    pre-fee signed cash effect (settlement rows only, per this module)
  - `net_value: Decimal`        -- ALWAYS present; `value` minus this row's
    own fee (signed) -- recorded as `ExternalFillImported.value`
  - `executed_at: datetime`     -- the fill's OWN broker-reported timestamp
  - `regulatory_fees` / `clearing_fees` / `commission` /
    `proprietary_index_option_fees` (all `Decimal | None`) -- the SDK's
    `set_sign_for` validator makes these NEGATIVE (debit-effect) at parse
    time, so their sum is a signed cost; `_fee_total` below reports the
    POSITIVE magnitude to match this codebase's existing fee convention
    (`ShortStopped.fee`, `CondorFilled.fee`, etc. are all positive costs).
    A settlement row's fee is instead `abs(net_value - value)` -- both
    fields are always present on that row, so this is never a guess.

RPT-16(5) idempotency (REWORKED 2026-07-10, operator ruling): a day-level
"already_imported" short-circuit cannot add a settlement that posts to the
broker's ledger on a LATER run (settlements sometimes land next-day -- see
`day_settlements`), so identity is transaction-level: `(at, symbol, action)`
for every `ExternalFillImported` already recorded for `day`. Re-running the
import always re-fetches (fills and settlements alike) and appends only the
rows whose key isn't already present -- a day fully imported becomes a
true no-op (nothing appended), while a day imported fills-only before
settlements posted picks up exactly the missing settlement rows.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Protocol

from meic.domain.events import Event, ExternalFillImported


class BackfillBrokerFacade(Protocol):
    """The ONLY broker surface RPT-16 backfill may touch -- two read fetches.
    No submit/replace/cancel method exists on this type at all."""

    async def day_fills(self, day: str) -> list[Any]: ...
    async def day_settlements(self, day: str) -> list[Any]: ...


def _fee_total(t: Any) -> Decimal | None:
    """One Trade-row transaction's total fee, as a POSITIVE cost. None only
    when the broker reported no fee data at all for this leg (honest
    absence, never fabricated as 0)."""
    names = ("regulatory_fees", "clearing_fees", "commission", "proprietary_index_option_fees")
    values = [getattr(t, name, None) for name in names]
    if all(v is None for v in values):
        return None
    total = sum((Decimal(str(v)) for v in values if v is not None), Decimal("0"))
    return abs(total)


def _order_id_of(t: Any) -> str | None:
    order_id = getattr(t, "order_id", None)
    return None if order_id is None else str(order_id)


def _symbol_of(t: Any) -> str:
    symbol = getattr(t, "symbol", None)
    return "" if symbol is None else str(symbol)


def _at_of(t: Any) -> str:
    executed_at = getattr(t, "executed_at", None)
    return executed_at.isoformat() if hasattr(executed_at, "isoformat") else str(executed_at)


async def backfill_day(
    events: list[Event],
    broker_reads: BackfillBrokerFacade,
    day: str,
    order_ids: set[str],
    *,
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    """RPT-16(5): idempotent (transaction-level -- see module docstring) +
    auditable.

    RPT-16(2)/OWN-03: only a broker Trade fill whose order id is in the
    operator-supplied `order_ids` becomes an event; every other Trade fill
    returned for that day (the operator's own trading, or a non-fill Trade
    row with no order id at all) is counted `skipped_foreign` and never
    imported.

    Settlement rows (operator ruling 2026-07-10) are matched by SYMBOL, not
    order id -- a Receive-Deliver transaction generally carries no order id
    at all. A settlement symbol is imported only when it belongs to one of
    OUR matched orders (this run or an earlier run for this `day`) AND was
    never ALSO seen on a foreign order's Trade row today -- that overlap
    would make the settlement's ownership genuinely ambiguous (OWN-03), so
    it is skipped and counted `ambiguous_settlements` rather than guessed.
    """
    wanted = {str(oid) for oid in order_ids}
    imported_at = now_iso()

    existing_keys = {(e.at, e.symbol, e.action)
                     for e in events if isinstance(e, ExternalFillImported) and e.day == day}
    # symbol -> the order id that owns it, seeded from any earlier run for
    # this day so a later, settlements-only run still knows which symbols
    # are ours without re-deciding order-id membership.
    own_symbols: dict[str, str] = {
        e.symbol: e.order_id for e in events
        if isinstance(e, ExternalFillImported) and e.day == day and e.order_id in wanted
    }
    foreign_symbols: set[str] = set()

    fills = await broker_reads.day_fills(day)
    imported = 0
    skipped_foreign = 0

    for t in fills:
        order_id = _order_id_of(t)
        symbol = _symbol_of(t)
        if order_id is None or order_id not in wanted:
            skipped_foreign += 1
            if symbol:
                foreign_symbols.add(symbol)
            continue

        action = "" if getattr(t, "action", None) is None else str(t.action)
        at = _at_of(t)
        own_symbols[symbol] = order_id
        key = (at, symbol, action)
        if key in existing_keys:
            continue

        price = getattr(t, "price", None)
        quantity = getattr(t, "quantity", None)
        events.append(ExternalFillImported(
            day=day, at=at, order_id=order_id, symbol=symbol, action=action,
            quantity=0 if quantity is None else int(quantity),
            price=None if price is None else Decimal(str(price)),
            fee=_fee_total(t),
            imported_at=imported_at,
            source="tastytrade_history",
        ))
        existing_keys.add(key)
        imported += 1

    # OWN-03 attribution limit (acknowledged, operator ruling 2026-07-10):
    # symbol-scoped matching attributes a settlement to OUR order only when
    # that symbol was not ALSO traded by a foreign position the same day --
    # a Receive-Deliver row carries no order id, so a shared symbol's
    # settlement cash is genuinely unattributable from broker data alone.
    # For the one day this rule was ratified for (2026-07-09, order
    # 482390058: P7535/P7510/C7540/C7565) all four symbols are unshared, so
    # the guard below never fires there. The GENERAL fix (e.g. quantity
    # apportionment or an operator-supplied attribution) needs a future
    # ruling; until then an overlapping symbol is SKIPPED and surfaced in
    # `ambiguous_settlements` -- never guessed.
    settlements = await broker_reads.day_settlements(day)
    settlements_imported = 0
    ambiguous_settlements = 0

    for t in settlements:
        symbol = _symbol_of(t)
        if symbol not in own_symbols:
            continue  # not one of our matched positions -- not ours to import
        if symbol in foreign_symbols:
            ambiguous_settlements += 1
            continue

        sub_type = getattr(t, "transaction_sub_type", None)
        action = "" if sub_type is None else str(sub_type)
        at = _at_of(t)
        key = (at, symbol, action)
        if key in existing_keys:
            continue

        raw_value = getattr(t, "value", None)
        net_value = getattr(t, "net_value", None)
        value = None if net_value is None else Decimal(str(net_value))
        fee = (None if raw_value is None or net_value is None
               else abs(Decimal(str(net_value)) - Decimal(str(raw_value))))
        price = getattr(t, "price", None)
        quantity = getattr(t, "quantity", None)

        events.append(ExternalFillImported(
            day=day, at=at, order_id=own_symbols[symbol], symbol=symbol, action=action,
            quantity=0 if quantity is None else int(quantity),
            price=None if price is None else Decimal(str(price)),
            fee=fee, value=value,
            imported_at=imported_at,
            source="tastytrade_history",
        ))
        existing_keys.add(key)
        settlements_imported += 1

    return {
        "result": "imported", "fills": imported, "skipped_foreign": skipped_foreign,
        "settlements": settlements_imported, "ambiguous_settlements": ambiguous_settlements,
    }
