"""LegBook — ORD-09 (v1.45): broker-truth leg identity, read from the event log.

Every order action AFTER the fill — stop placement (STP-01), LEX, DCY buybacks,
CLS closes, flatten, watchdog escalations — takes its instrument symbol from
here, and here reads only what the broker itself reported at fill time.

Why this exists: before ORD-09, four services identified legs as the bare string
`"short_put"`, and `panel_commands.close()` invented symbols like
`"2026-07-07#1:PUT"`. Against cert those orders name an instrument that does not
exist. Reconstructing the symbol from strike/expiry/right at ACTION time is not
the fix either: it re-runs symbology math on every use, which is the same drift
class as the intent-translation defect. Reconstruction is a cross-check that
ALERTS on mismatch, never the source.

Also the data source for the STP-02d reconciliation records and the OWN
fill-derived ledger (ORD-09).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from meic.domain.events import CondorFilled, Event, FilledLeg


@dataclass(frozen=True)
class LegBook:
    """entry_id -> the legs the broker said it filled."""

    legs: dict[str, tuple[FilledLeg, ...]]

    @staticmethod
    def from_events(events: list[Event]) -> "LegBook":
        book: dict[str, tuple[FilledLeg, ...]] = {}
        for e in events:
            if isinstance(e, CondorFilled) and e.legs:
                book[e.entry_id] = e.legs
        return LegBook(book)

    def of(self, entry_id: str) -> tuple[FilledLeg, ...]:
        return self.legs.get(entry_id, ())

    def leg(self, entry_id: str, side: str, role: str) -> FilledLeg | None:
        """The one leg of this entry on `side` ("PUT"/"CALL") in `role`."""
        for leg in self.of(entry_id):
            if leg.side == side and leg.role == role:
                return leg
        return None

    def symbol(self, entry_id: str, side: str, role: str = "short") -> str | None:
        leg = self.leg(entry_id, side, role)
        return leg.symbol if leg else None

    def shorts(self, entry_id: str) -> tuple[FilledLeg, ...]:
        return tuple(l for l in self.of(entry_id) if l.role == "short")

    def open_sides(self, entry_id: str) -> tuple[str, ...]:
        return tuple(sorted({l.side for l in self.shorts(entry_id)}))


def crosscheck_leg_symbols(
    recorded: tuple[FilledLeg, ...],
    *,
    underlying: str,
    expiration: date,
    strikes: dict[tuple[str, str], Decimal],   # (right, role) -> strike
) -> list[str]:
    """ORD-09: reconstruction is permitted ONLY as a cross-check that alerts on
    mismatch — never as the source.

    Returns a list of human-readable mismatches, naming BOTH values. The caller
    alerts; the caller then goes on using the RECORDED symbol regardless. A
    mismatch means our symbology or our idea of the strikes has drifted from the
    broker's — which is exactly the thing we must not silently paper over by
    "correcting" the broker.
    """
    from meic.adapters.occ import occ_symbol

    problems: list[str] = []
    for leg in recorded:
        strike = strikes.get((leg.right, leg.role))
        if strike is None:
            continue                                  # nothing to check against
        try:
            expected = occ_symbol(underlying, expiration, leg.right, strike)
        except ValueError as e:                       # unrepresentable strike
            problems.append(f"{leg.symbol}: cannot reconstruct ({e})")
            continue
        if expected != leg.symbol:
            problems.append(
                f"{leg.role} {leg.side}: broker reported {leg.symbol!r}, "
                f"reconstruction from strike {strike} gives {expected!r}")
    return problems
