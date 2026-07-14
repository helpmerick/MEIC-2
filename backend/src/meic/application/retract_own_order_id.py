"""OWN-01 append-only retraction: a one-off, operator-triggered append of
`OwnOrderIdRetracted` events for a broker order id that was mistakenly
claimed as one of the bot's own (either directly on a fill-bearing event, or
via `application/backfill_order_ids.backfill_own_order_ids`), so
`reporting/own_orders.py::own_order_ids` withdraws it from the bot's
own-scope going forward (see that module and `OwnOrderIdRetracted`'s
docstring in `domain/events.py`).

Pure event append -- no broker calls, no I/O, no submit/replace/cancel
capability anywhere in this module (mirrors the structural guarantee
`application/backfill_order_ids.py` gives for the same reason).

Idempotent: an id already retracted for this `entry_id` (matched by
`broker_order_id`, regardless of `reason`) is skipped, so running the
retraction twice appends nothing the second time.

Never deletes or rewrites the mistaken `OwnOrderIdBackfilled` (or any other)
event -- the log is append-only; this only appends a NEW event whose sole
effect is on `own_order_ids`.
"""
from __future__ import annotations

from typing import Iterable

from meic.domain.events import Event, OwnOrderIdRetracted


def retract_own_order_ids(
    events: list[Event],
    entry_id: str,
    ids: Iterable[tuple[str, str]],
    *,
    at: str,
    note: str,
) -> int:
    """Append one `OwnOrderIdRetracted` per (broker_order_id, reason) in
    `ids` not already retracted for `entry_id`. Returns the number
    appended."""
    existing = {e.broker_order_id for e in events
                if isinstance(e, OwnOrderIdRetracted) and e.entry_id == entry_id}
    appended = 0
    for broker_order_id, reason in ids:
        if broker_order_id in existing:
            continue
        events.append(OwnOrderIdRetracted(
            entry_id=entry_id, broker_order_id=broker_order_id, reason=reason,
            at=at, note=note))
        existing.add(broker_order_id)
        appended += 1
    return appended
