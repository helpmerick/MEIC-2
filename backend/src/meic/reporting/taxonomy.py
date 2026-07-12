"""RPT-03 outcome taxonomy & contract audit.

Every CLOSED/settled entry classifies EXACTLY ONCE into one of the outcomes
below (ten pre-v1.58, plus TPT_CLOSE for the v1.58 take-profit target). The contract audit is the standing inequality check born of
the v1.38 Ash-ratified `total_credit` outcome contract (doc 01 STP-02): a
ONE_SIDE_STOPPED entry must realize at least (1 − pct) × credit minus
recorded slippage; a BOTH_SIDES_STOPPED entry at least −(2·pct − 1) × credit
minus recorded slippage. A breach is a RED FLAG — expected count zero,
forever (doc 10 RPT-03).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from meic.domain.projection import EntryProjection

from .folds import entry_credit_dollars, entry_dollars

FULL_EXPIRY = "FULL_EXPIRY"
ONE_SIDE_STOPPED = "ONE_SIDE_STOPPED"
BOTH_SIDES_STOPPED = "BOTH_SIDES_STOPPED"
TPF_CLOSE = "TPF_CLOSE"
TPT_CLOSE = "TPT_CLOSE"  # v1.58: take-profit TARGET close, distinct from the TPF floor
DECAY_CLOSE = "DECAY_CLOSE"
MANUAL_CLOSE = "MANUAL_CLOSE"
MANUAL_FLATTEN = "MANUAL_FLATTEN"
EOD_CLOSE = "EOD_CLOSE"
INFEASIBLE_STOP = "INFEASIBLE_STOP"
EXTERNAL = "EXTERNAL"

# CLS-02/CLS-04 initiators that map directly to one outcome each. `decay` is
# handled separately below (DCY-02's short-only close can ALSO show up via
# `stop_initiators` when the entry's other side stops normally — either
# signal means DECAY_CLOSE), and an initiator this taxonomy doesn't
# recognize (e.g. a genuinely external, operator-at-the-broker action, D5)
# falls through to EXTERNAL rather than raising — an unrecognized initiator
# is exactly what EXTERNAL exists to catch.
_INITIATOR_OUTCOME = {
    "manual": MANUAL_CLOSE,
    "manual_flatten": MANUAL_FLATTEN,
    "take_profit": TPF_CLOSE,
    "take_profit_target": TPT_CLOSE,
    "eod": EOD_CLOSE,
    "infeasible_stop": INFEASIBLE_STOP,
}


def classify(entry: EntryProjection) -> str | None:
    """Exactly one outcome, or None if the entry has not yet settled/closed
    (still open — not a reportable outcome yet)."""
    if entry.close_initiator == "decay" or "decay" in entry.stop_initiators:
        return DECAY_CLOSE
    if entry.close_initiator is not None:
        return _INITIATOR_OUTCOME.get(entry.close_initiator, EXTERNAL)
    if len(entry.sides_stopped) >= 2:
        return BOTH_SIDES_STOPPED
    if len(entry.sides_stopped) == 1:
        return ONE_SIDE_STOPPED
    if len(entry.sides_expired) >= 2:
        return FULL_EXPIRY
    return None


@dataclass(frozen=True)
class ContractBreach:
    entry_id: str
    outcome: str
    realized: Decimal   # real dollars
    floor: Decimal      # real dollars — the contract's minimum promise, before slippage
    slippage_allowance: Decimal


def contract_audit(entry: EntryProjection, *, pct: Decimal,
                    slippage_allowance: Decimal = Decimal("0")) -> ContractBreach | None:
    """The v1.38 total_credit contract-audit inequalities (doc 10 RPT-03).
    `pct` is the entry's `stop_loss_pct` as a FRACTION (e.g. Decimal("0.95")
    for 95%). Returns a `ContractBreach` only when the realized result fell
    below the contractual floor net of the recorded slippage allowance; the
    audit does not apply to any other outcome (returns None)."""
    outcome = classify(entry)
    if outcome not in (ONE_SIDE_STOPPED, BOTH_SIDES_STOPPED):
        return None
    credit = entry_credit_dollars(entry)
    realized = entry_dollars(entry)
    if outcome == ONE_SIDE_STOPPED:
        floor = (Decimal("1") - pct) * credit
    else:  # BOTH_SIDES_STOPPED
        floor = -((Decimal("2") * pct) - Decimal("1")) * credit
    minimum = floor - slippage_allowance
    if realized < minimum:
        return ContractBreach(entry_id=entry.entry_id, outcome=outcome,
                               realized=realized, floor=floor,
                               slippage_allowance=slippage_allowance)
    return None
