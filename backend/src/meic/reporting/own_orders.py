"""OWN-01/OWN-03: the bot's own broker order ids, read generically off the
event log. Pure (no I/O, no adapter imports) so both `application/` (which
may not import `meic.adapters`) and `adapters/api/server.py` can share ONE
definition instead of two independently-drifting copies.
"""
from __future__ import annotations

from typing import Any, Iterable


def own_order_ids(events: Iterable[Any]) -> set[str]:
    """Every broker order id the bot itself journaled placing — read
    generically off ANY event carrying `broker_order_id` (StopPlaced v1.60,
    DecayBuybackPlaced v1.61, LexOrderPlaced v1.62, and CondorFilled — the
    entry order, added here). On a shared account (v1.49) this is what
    separates the bot's own fills from the operator's."""
    return {str(v) for e in events
            if (v := getattr(e, "broker_order_id", None)) is not None}
