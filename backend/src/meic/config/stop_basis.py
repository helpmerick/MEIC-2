"""stop_basis config validation — STP-02d gate (v1.43).

`per_side` is GATED: config validation rejects it (reason `allocation_unverified`)
globally and per-entry, with NO runtime toggle — lifting it is a spec
amendment. `total_credit` and `short_premium` remain selectable. The per_side
formulas stay in the domain (stop_policy); only SELECTION is blocked here.
"""
from __future__ import annotations

VALID_BASES = ("total_credit", "short_premium", "per_side")  # per_side exists but is gated
SELECTABLE_BASES = ("total_credit", "short_premium")          # what config validation accepts


class StopBasisRejected(ValueError):
    def __init__(self, basis: str, reason: str) -> None:
        self.basis, self.reason = basis, reason
        super().__init__(f"stop_basis {basis!r} rejected: {reason}")


def validate_stop_basis(basis: str) -> None:
    """Raise StopBasisRejected if `basis` may not be selected. No parameter,
    env var, or flag lifts the per_side gate — it is spec-level (STP-02d)."""
    if basis not in VALID_BASES:
        raise StopBasisRejected(basis, "unknown_basis")
    if basis == "per_side":
        raise StopBasisRejected(basis, "allocation_unverified")  # STP-02d
