"""PNL-01 fee computation -- the ONE function every fill-event construction
site calls to price a leg fill: `CondorFilled` (entry, all four legs),
`ShortStopped` (a stop buyback / watchdog escalation / decay buyback, one
short leg closing), `LongSold` (a LEX sale, one long leg closing).

Pure domain (no I/O, CLAUDE.md rule 6): given the `FeeModel` in force and the
already-fetched leg data callers already hold (ORD-09's `FilledLeg.role`),
returns the fee to RECORD on that event. Recording (never recomputing later)
keeps replay deterministic (PNL-03) -- the fee in force at fill time is what
a replay must reproduce, exactly like `net_credit`.

PER-SHARE, deliberately NOT scaled by contracts (see
`FeeModel.per_share_fee`'s docstring): `CondorFilled.net_credit`,
`ShortStopped.fill` and `LongSold.recovery` are all per-share amounts --
`reporting/folds.py::entry_dollars` applies `* 100 * contracts` exactly ONCE,
for the whole entry. A real per-contract dollar fee is already linear in
contracts, so recording it here PRE-multiplied by contracts and then having
the reporting layer multiply by contracts AGAIN would double-count -- exactly
the class of bug the PNL-01 build-time gate exists to catch.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from meic.config.fee_model import FeeModel


def fee_for_leg(model: FeeModel, *, role: str, opening: bool) -> Decimal:
    """THE fee function. The PER-SHARE fee for filling one leg.

    `role` is "short" | "long"; `opening` is True for an entry fill, False
    for any closing fill (stop buyback, watchdog escalation, decay buyback,
    LEX sale, manual close)."""
    return model.per_share_fee(role=role, opening=opening)


def fee_for_legs(model: FeeModel, legs: Iterable, *, opening: bool) -> Decimal:
    """The SAME function, summed over multiple legs -- `CondorFilled`'s
    four-leg entry fill (two different roles, hence two different rates).
    `legs` are FilledLeg-shaped (`.role`); no new computation, just
    `fee_for_leg` applied per leg and totalled."""
    return sum(
        (fee_for_leg(model, role=leg.role, opening=opening) for leg in legs),
        Decimal("0"),
    )
