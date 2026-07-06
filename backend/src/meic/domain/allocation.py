"""Allocation reconciliation — STP-02d passive evidence + ungate criterion.

On EVERY real (non-paper) condor fill, in ALL bases, the adapter logs a record
comparing Σ(allocated leg prices) to the net fill: PASS iff they agree within
one tick and no leg is zero-priced unless it actually traded at zero. Evidence
accumulates from normal trading. The per_side gate lifts only after 5
CONSECUTIVE PASSED records — a FAIL resets the streak — AND a ratified spec
amendment (the amendment is not automatic; this module only reports readiness).

Pure: paper fills never reach here (the SimulatedBroker does not call it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class AllocationRecord:
    passed: bool
    reason: str
    allocated_sum: Decimal
    net_fill: Decimal


def reconcile(
    allocated_leg_prices: list[Decimal],
    net_fill: Decimal,
    *,
    tick: Decimal,
    legs_that_traded_at_zero: frozenset[int] = frozenset(),
) -> AllocationRecord:
    """One allocation-reconciliation record for a real fill (STP-02d.2)."""
    total = sum(allocated_leg_prices, Decimal("0"))
    for i, price in enumerate(allocated_leg_prices):
        if price == 0 and i not in legs_that_traded_at_zero:
            return AllocationRecord(False, "phantom_zero_priced_leg", total, net_fill)
    if abs(total - net_fill) > tick:
        return AllocationRecord(False, "sum_mismatch", total, net_fill)
    return AllocationRecord(True, "ok", total, net_fill)


@dataclass
class AllocationGate:
    """Tracks the STP-02d ungate criterion: 5 consecutive PASSED real-fill
    records. A FAIL resets the streak. This reports readiness only — actually
    lifting the gate still requires an operator-ratified amendment."""

    required: int = 5
    _consecutive_passed: int = 0
    records: list[AllocationRecord] = field(default_factory=list)

    def observe(self, record: AllocationRecord) -> None:
        self.records.append(record)
        if record.passed:
            self._consecutive_passed += 1
        else:
            self._consecutive_passed = 0  # a FAIL resets the streak

    @property
    def consecutive_passed(self) -> int:
        return self._consecutive_passed

    def ungate_ready(self) -> bool:
        """True when the empirical bar is met. The gate itself (config
        rejection) still stands until a ratified amendment removes it."""
        return self._consecutive_passed >= self.required
