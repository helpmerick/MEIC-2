"""RPT-16 (proposed amendment — AMENDMENT-PROPOSAL-historical-backfill.md):
the one-time, operator-triggered import of pre-journal broker history into
the durable event log. Imported history enters as `ExternalFillImported`
events, one per broker fill leg -- NEVER as a synthetic `CondorFilled`: the
log is authoritative for INTENT (REC-02), and an imported row carries no
recorded intent, only the broker's own record of what happened.

Structurally read-only at the broker (RPT-16 rule 6): `broker_reads` is a
narrow, duck-typed facade exposing only `day_fills(day)` -- mirrors
`ReadOnlyBrokerFacade` in application/report_reconciler.py, never a real
order-capable adapter. This module imports nothing from `meic.adapters` or
`meic.composition` and references no submit/replace/cancel capability
anywhere (see tests/application/test_backfill_structural.py, which asserts
this the same way tests/application/test_report_reconciler_structural.py
does for the reconciler).

Broker `Transaction` field mapping (tastytrade SDK, `tastytrade/account.py`,
verified against the installed .venv copy) that this module reads:
  - `order_id: int | None`      -- OWN-03 match key (compared as `str`)
  - `symbol: str | None`        -- the broker's own instrument symbol
  - `action: OrderAction | None` -- a StrEnum ("Sell to Open" | "Buy to Open" |
    "Sell to Close" | "Buy to Close" | ...); `str(action)` IS the value text
  - `quantity: Decimal | None`
  - `price: Decimal | None`     -- broker-allocated fill price
  - `executed_at: datetime`     -- the fill's OWN broker-reported timestamp
  - `regulatory_fees` / `clearing_fees` / `commission` /
    `proprietary_index_option_fees` (all `Decimal | None`) -- the SDK's
    `set_sign_for` validator makes these NEGATIVE (debit-effect) at parse
    time, so their sum is a signed cost; `_fee_total` below reports the
    POSITIVE magnitude to match this codebase's existing fee convention
    (`ShortStopped.fee`, `CondorFilled.fee`, etc. are all positive costs).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Protocol

from meic.domain.events import Event, ExternalFillImported


class BackfillBrokerFacade(Protocol):
    """The ONLY broker surface RPT-16 backfill may touch -- one read fetch.
    No submit/replace/cancel method exists on this type at all."""

    async def day_fills(self, day: str) -> list[Any]: ...


def _fee_total(t: Any) -> Decimal | None:
    """One transaction's total fee, as a POSITIVE cost. None only when the
    broker reported no fee data at all for this leg (honest absence, never
    fabricated as 0)."""
    names = ("regulatory_fees", "clearing_fees", "commission", "proprietary_index_option_fees")
    values = [getattr(t, name, None) for name in names]
    if all(v is None for v in values):
        return None
    total = sum((Decimal(str(v)) for v in values if v is not None), Decimal("0"))
    return abs(total)


def _order_id_of(t: Any) -> str | None:
    order_id = getattr(t, "order_id", None)
    return None if order_id is None else str(order_id)


async def backfill_day(
    events: list[Event],
    broker_reads: BackfillBrokerFacade,
    day: str,
    order_ids: set[str],
    *,
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    """RPT-16(5): idempotent + auditable. Re-running for a `day` that already
    carries an `ExternalFillImported` is a no-op (never a duplicate import).

    RPT-16(2)/OWN-03: only a broker fill whose order id is in the operator-
    supplied `order_ids` becomes an event; every other fill returned for that
    day (the operator's own trading, or a non-fill Trade row with no order id
    at all -- e.g. cash settlement, EOD-01) is counted `skipped_foreign` and
    never imported.
    """
    already = [e for e in events if isinstance(e, ExternalFillImported) and e.day == day]
    if already:
        return {"result": "already_imported", "count": len(already)}

    wanted = {str(oid) for oid in order_ids}
    fills = await broker_reads.day_fills(day)
    imported_at = now_iso()
    imported = 0
    skipped_foreign = 0

    for t in fills:
        order_id = _order_id_of(t)
        if order_id is None or order_id not in wanted:
            skipped_foreign += 1
            continue

        symbol = getattr(t, "symbol", None)
        action = getattr(t, "action", None)
        quantity = getattr(t, "quantity", None)
        price = getattr(t, "price", None)
        executed_at = getattr(t, "executed_at", None)

        events.append(ExternalFillImported(
            day=day,
            at=executed_at.isoformat() if hasattr(executed_at, "isoformat") else str(executed_at),
            order_id=order_id,
            symbol="" if symbol is None else str(symbol),
            action="" if action is None else str(action),
            quantity=0 if quantity is None else int(quantity),
            price=None if price is None else Decimal(str(price)),
            fee=_fee_total(t),
            imported_at=imported_at,
            source="tastytrade_history",
        ))
        imported += 1

    return {"result": "imported", "fills": imported, "skipped_foreign": skipped_foreign}
