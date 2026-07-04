"""Per-side state machine — the legality table from doc 05 §3, verbatim.

"The legality table IS the spec — transitions not listed are bugs."

PHASE-3 SCOPE NOTE (operator-directed): this module encodes the TABLE only —
pure states and transition legality. Everything that DRIVES stop-related
transitions (stop placement, protection confirmation, UNPROTECTED
escalation/retry policy) is FROZEN until the STP-05a findings report is
reviewed and the Phase 2 PR is merged. No behavior here places, prices, or
retries anything.
"""
from __future__ import annotations

from enum import Enum


class SideState(str, Enum):
    PENDING = "PENDING"
    WORKING = "WORKING"
    SKIPPED = "SKIPPED"
    PARTIAL_RESOLVING = "PARTIAL_RESOLVING"
    OPEN_UNCONFIRMED_STOP = "OPEN_UNCONFIRMED_STOP"
    PROTECTED = "PROTECTED"
    UNPROTECTED = "UNPROTECTED"
    FLATTENED = "FLATTENED"
    SIDE_STOPPED = "SIDE_STOPPED"
    LONG_LIQUIDATING = "LONG_LIQUIDATING"
    SIDE_CLOSED = "SIDE_CLOSED"
    DECAY_CLOSING = "DECAY_CLOSING"
    SIDE_CLOSED_DECAY = "SIDE_CLOSED_DECAY"
    SIDE_EXPIRED = "SIDE_EXPIRED"
    MANUAL = "MANUAL"
    SUSPENDED = "SUSPENDED"


# doc 05 §3, transcribed line by line:
_TABLE: frozenset[tuple[SideState, SideState]] = frozenset({
    (SideState.PENDING, SideState.WORKING),
    (SideState.WORKING, SideState.OPEN_UNCONFIRMED_STOP),
    (SideState.OPEN_UNCONFIRMED_STOP, SideState.PROTECTED),
    (SideState.WORKING, SideState.SKIPPED),
    (SideState.WORKING, SideState.PARTIAL_RESOLVING),
    (SideState.PARTIAL_RESOLVING, SideState.PROTECTED),
    (SideState.PARTIAL_RESOLVING, SideState.FLATTENED),
    (SideState.OPEN_UNCONFIRMED_STOP, SideState.UNPROTECTED),
    (SideState.UNPROTECTED, SideState.PROTECTED),
    (SideState.UNPROTECTED, SideState.FLATTENED),
    (SideState.PROTECTED, SideState.SIDE_STOPPED),
    (SideState.SIDE_STOPPED, SideState.LONG_LIQUIDATING),
    (SideState.LONG_LIQUIDATING, SideState.SIDE_CLOSED),
    (SideState.PROTECTED, SideState.DECAY_CLOSING),               # DCY-02/03
    (SideState.DECAY_CLOSING, SideState.SIDE_CLOSED_DECAY),
    (SideState.SIDE_CLOSED_DECAY, SideState.SIDE_EXPIRED),
    (SideState.DECAY_CLOSING, SideState.PROTECTED),               # re-inflation guard
    (SideState.PROTECTED, SideState.SIDE_EXPIRED),
    (SideState.LONG_LIQUIDATING, SideState.SIDE_EXPIRED),         # EOD-04
})

_ANY_TO = {SideState.MANUAL, SideState.SUSPENDED}  # UC-08; OWN-06/09/10/11


def can_transition(src: SideState, dst: SideState) -> bool:
    """True iff the doc 05 §3 table lists the transition. MANUAL and SUSPENDED
    are reachable from any state; resumption from them is reconciliation-
    determined (application layer, out of Phase-3 scope)."""
    if dst in _ANY_TO:
        return True
    return (src, dst) in _TABLE


def assert_transition(src: SideState, dst: SideState) -> None:
    """Raise on an illegal transition — 'transitions not listed are bugs'."""
    if not can_transition(src, dst):
        raise ValueError(f"illegal side-state transition: {src.value} -> {dst.value} (doc 05 §3)")
