"""Cancel-outcome routing — EC-API-06.

A cancel request the broker rejects *because the order already filled* is not a
failure: broker truth wins (LEX-08). The fill is real, so the outcome routes to
the fill handler (EC-ENT-12 for entry orders, EC-LEX-03 for LEX replaces) —
never bought back or cancelled twice. Any other rejection is a genuine cancel
failure the caller must handle.
"""
from __future__ import annotations

# Reasons a broker gives when a cancel loses the race to a fill.
_FILLED_REASONS = frozenset({"already_filled", "order_filled", "filled", "order_not_cancellable"})


def route_cancel_outcome(*, rejected: bool, reason: str = "") -> str:
    """Classify a cancel response into one of:
      "cancelled"      — the cancel took effect; the order is dead.
      "route_as_fill"  — rejected because already filled ⇒ treat as a fill
                         (EC-API-06 → EC-ENT-12 / EC-LEX-03).
      "cancel_failed"  — rejected for any other reason; caller handles it.
    """
    if not rejected:
        return "cancelled"
    if reason.strip().lower() in _FILLED_REASONS:
        return "route_as_fill"  # EC-API-06: broker truth — the order filled
    return "cancel_failed"
