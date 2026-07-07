"""Idempotency-keyed resubmission — ORD-04 / REC-05 (pure decision).

Every order carries a client-generated idempotency key. A submit whose response
times out may nonetheless have landed at the broker. Before resubmitting, the
bot queries the broker by that key: if the order already exists it is adopted
and NOT resubmitted, so a timeout can never produce a duplicate order (the bug
that motivated ORD-04).
"""
from __future__ import annotations

from typing import Iterable


def resolve_submit_after_timeout(idempotency_key: str,
                                 existing_order_keys: Iterable[str]) -> dict:
    """Return {"exists": bool, "resubmit": bool}. If the key is already present
    at the broker, adopt the existing order and do not resubmit."""
    exists = idempotency_key in set(existing_order_keys)
    return {"exists": exists, "resubmit": not exists}
