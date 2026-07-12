"""Entry partial-fill resolution — EC-ENT-06 (pure decision).

ORD-01 fills the condor as a single 4-leg complex order, all-or-nothing per
contract, so a "partial" means fewer COMPLETE condors than ordered — never
unbalanced legs. The filled condors are kept and protected (STP-01) at the
reduced quantity. If reconciliation ever finds unbalanced legs (a broker
anomaly that should be impossible), completion is attempted for
partial_fix_seconds, else the filled legs are flattened; an unbalanced position
is never carried past partial_fix_seconds × 2, and any discovery is a critical
alert.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartialPlan:
    keep_condors: int      # complete condors kept and protected
    place_stops: bool      # STP-01 on the kept condors
    recorded_qty: int      # the reduced quantity recorded


def resolve_balanced_partial(*, ordered_condors: int, filled_condors: int) -> PartialPlan:
    """EC-ENT-06 (a): a balanced partial — keep the filled condors, place their
    stops, record the reduced quantity. Zero filled ⇒ nothing to protect."""
    kept = max(0, min(filled_condors, ordered_condors))
    return PartialPlan(keep_condors=kept, place_stops=kept > 0, recorded_qty=kept)


def resolve_unbalanced(*, seconds_since_detect: float, partial_fix_seconds: float) -> dict:
    """EC-ENT-06 (b): an unbalanced-leg anomaly — attempt completion for
    partial_fix_seconds, else flatten the filled legs; NEVER carry past
    partial_fix_seconds × 2. Any discovery raises a critical alert."""
    alert = ("critical", "unbalanced_position")
    if seconds_since_detect < partial_fix_seconds:
        return {"action": "attempt_completion", "alert": alert}
    return {"action": "flatten_filled_legs", "alert": alert,
            "hard_cap": seconds_since_detect >= partial_fix_seconds * 2}
