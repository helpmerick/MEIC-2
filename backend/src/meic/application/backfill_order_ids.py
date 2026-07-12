"""OWN-03 / RPT-16 escape hatch: a one-off, operator-triggered append of
`OwnOrderIdBackfilled` events for an entry whose original events (CondorFilled/
StopPlaced/LexOrderPlaced, ...) predate order-id journaling, so
`application/report_reconciler.py` can scope that day's broker rows to the
bot's own trades (see that module and `reporting/own_orders.py`).

Pure event append -- no broker calls, no I/O, no submit/replace/cancel
capability anywhere in this module (mirrors the structural guarantee
`application/backfill.py` and `application/report_reconciler.py` already give
for RPT-16/RPT-15).

Idempotent: an id already journaled for this `entry_id` (matched by
`broker_order_id`, regardless of role) is skipped, so running the backfill
twice appends nothing the second time.
"""
from __future__ import annotations

from typing import Iterable

from meic.domain.events import Event, OwnOrderIdBackfilled


def backfill_own_order_ids(
    events: list[Event],
    entry_id: str,
    ids: Iterable[tuple[str, str]],
    *,
    at: str,
    note: str,
) -> int:
    """Append one `OwnOrderIdBackfilled` per (broker_order_id, role) in `ids`
    not already journaled for `entry_id`. Returns the number appended."""
    existing = {e.broker_order_id for e in events
                if isinstance(e, OwnOrderIdBackfilled) and e.entry_id == entry_id}
    appended = 0
    for broker_order_id, role in ids:
        if broker_order_id in existing:
            continue
        events.append(OwnOrderIdBackfilled(
            entry_id=entry_id, broker_order_id=broker_order_id, role=role,
            at=at, note=note))
        existing.add(broker_order_id)
        appended += 1
    return appended
