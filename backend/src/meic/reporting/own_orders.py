"""OWN-01/OWN-03: the bot's own broker order ids, read generically off the
event log. Pure (no I/O, no adapter imports) so both `application/` (which
may not import `meic.adapters`) and `adapters/api/server.py` can share ONE
definition instead of two independently-drifting copies.
"""
from __future__ import annotations

from typing import Any, Iterable

from meic.domain.events import OwnOrderIdRetracted


def own_order_ids(events: Iterable[Any]) -> set[str]:
    """Every broker order id the bot itself journaled placing — read
    generically off ANY event carrying `broker_order_id` (StopPlaced v1.60,
    DecayBuybackPlaced v1.61, LexOrderPlaced v1.62, and CondorFilled — the
    entry order, added here). On a shared account (v1.49) this is what
    separates the bot's own fills from the operator's.

    `OwnOrderIdRetracted` (OWN-01, 2026-07-14) withdraws a previously-claimed
    id -- e.g. an operator's own out-of-band order mistakenly backfilled as
    the bot's. CRITICAL TRAP: `OwnOrderIdRetracted` ITSELF carries a
    `broker_order_id` field, so it must be excluded from the "claimed" scan
    below (never counted as a claim of its own id) before the retracted set
    is subtracted -- otherwise the retraction event would re-claim the very
    id it exists to withdraw."""
    claimed: set[str] = set()
    retracted: set[str] = set()
    for e in events:
        v = getattr(e, "broker_order_id", None)
        if v is None:
            continue
        if isinstance(e, OwnOrderIdRetracted):
            retracted.add(str(v))
        else:
            claimed.add(str(v))
    return claimed - retracted
