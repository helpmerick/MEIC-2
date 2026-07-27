"""Ownership ledger — OWN-01..06 (pure domain).

The bot's per-symbol owned quantity is built EXCLUSIVELY from fills on its own
order IDs (OWN-01). foreign_delta = broker net − ledger (OWN-02). Anything not
attributable to the bot's own fills is FOREIGN and never touched (OWN-03),
including a foreign naked short. Every exit order is capped at the ledger
quantity (OWN-04, structural). A broker position SMALLER than the ledger is a
ledger shortfall ⇒ SUSPEND (OWN-06).

Signed convention: positive = long, negative = short. This module decides;
the single order-construction path consults cap_exit_qty so no adapter can
bypass it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Ownership(str, Enum):
    OWNED = "OWNED"        # bot's, broker agrees — manage normally
    SHARED = "SHARED"      # bot holds it AND operator traded it — constrain+warn (OWN-05)
    FOREIGN = "FOREIGN"    # not the bot's at all — quarantine, never touch (OWN-03)
    SHORTFALL = "SHORTFALL"  # broker shows less than ledger — SUSPEND (OWN-06)


@dataclass
class OwnershipLedger:
    _owned: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, int]:
        """REC-07 item 9: the OWN ledger is durable state — serialize it."""
        return dict(self._owned)

    @classmethod
    def restore(cls, snapshot: dict | None) -> "OwnershipLedger":
        """Rebuild from durable state on boot. An empty/absent snapshot means a
        fresh ledger — so every broker position is FOREIGN until the bot's own
        fills say otherwise (the safe direction)."""
        return cls(_owned={str(k): int(v) for k, v in (snapshot or {}).items()})

    @classmethod
    def from_events(cls, events) -> "OwnershipLedger":
        """OWN-03a: PROVABLE ownership, rebuilt from the journal (REC-07).

        The defect this closes: `apply_fill` had ZERO production call sites,
        so every ledger was empty and every own leg classified FOREIGN -- the
        exact desync that made an external operator's bot quarantine NINE of
        its own legs. Ownership is derived from ORD-10's journaled evidence
        (the CondorFilled legs the BROKER reported), never asserted ad hoc and
        never auto-adopted from broker positions (OWN-03's line).

        Derivation, per leg of each entry the journal shows still open:
          * entry closed (close_initiator set) -> nothing owned;
          * side expired or closed -> neither leg of that side;
          * side STOPPED -> the short was bought back; the LONG may or may not
            have been LEX-recovered, and `recoveries` is a SUM, not per-side,
            so which long was sold is NOT derivable -- the stopped side
            therefore contributes NOTHING. That deliberately UNDERSTATES:
            `cap_exit_qty`'s zero falls through to the caller's requested
            quantity and ORD-12's resolver (broker positions) is the real
            gate, so understating is contained while OVERSTATING would let a
            cap fail to bite. Unobserved means unassumed in the SAFE
            direction (NFR-09).
        """
        from meic.domain.projection import fold

        ledger = cls()
        for entry in fold(events).entries.values():
            if entry.close_initiator is not None:
                continue
            gone = set(entry.sides_stopped) | set(entry.sides_closed) | set(entry.sides_expired)
            for leg in entry.legs:
                if leg.side in gone:
                    continue
                ledger.apply_fill(leg.symbol, leg.qty if leg.role == "long" else -leg.qty)
        return ledger

    def refresh_from_events(self, events) -> None:
        """In-place re-derivation after a new fill. IN PLACE is the point
        (NFR-11): CloseEntry captured THIS object at construction, so a
        rebind on the composition would leave it holding a stale ledger --
        the alert-sink defect all over again. O(events) per call, and fills
        are at most a dozen a day."""
        rebuilt = OwnershipLedger.from_events(events)
        self._owned.clear()
        self._owned.update(rebuilt._owned)

    def apply_fill(self, symbol: str, signed_qty: int) -> None:
        """Record a fill on the bot's OWN order (OWN-01). Operator/manual
        trades never call this — they never enter the ledger."""
        self._owned[symbol] = self._owned.get(symbol, 0) + signed_qty
        if self._owned[symbol] == 0:
            del self._owned[symbol]

    def owned(self, symbol: str) -> int:
        return self._owned.get(symbol, 0)

    def foreign_delta(self, symbol: str, broker_net: int) -> int:
        """OWN-02: broker net position − bot ledger."""
        return broker_net - self.owned(symbol)

    def classify(self, symbol: str, broker_net: int) -> Ownership:
        ledger = self.owned(symbol)
        if ledger == 0:
            # Zero-quantity fix (2026-07-14, operator ruling): ledger 0 AND
            # broker_net 0 used to fall through to OWNED here, which
            # rendered a genuinely-flat symbol (e.g. a closed future/crypto
            # line the broker still lists at qty 0) as "adopted" in
            # reconcile_boot.py -- misleading, and it undermines trust in the
            # OWN-03 quarantine display. OWN-01: the ledger is built
            # EXCLUSIVELY from the bot's own fills; zero fills recorded means
            # the bot owns NONE of this symbol, full stop -- regardless of
            # what a stale/closed broker line happens to read, "not the
            # bot's own" is FOREIGN, the same as the broker_net != 0 case
            # just below. NON-zero-quantity classification (ledger != 0,
            # below) is completely untouched by this fix.
            return Ownership.FOREIGN
        if broker_net == ledger:
            return Ownership.OWNED
        # same-sign shrink below the ledger ⇒ operator closed bot lots (OWN-06)
        if (ledger > 0 and 0 <= broker_net < ledger) or (ledger < 0 and ledger < broker_net <= 0):
            return Ownership.SHORTFALL
        return Ownership.SHARED  # operator added lots on a shared symbol (OWN-05)

    def cap_exit_qty(self, symbol: str, requested_qty: int) -> int:
        """OWN-04: an exit order can never exceed the bot's ledger quantity.
        FOREIGN symbols (ledger 0) cap to 0 — the bot submits nothing."""
        return min(requested_qty, abs(self.owned(symbol)))

    def write_down_to(self, symbol: str, broker_net: int) -> None:
        """OWN-06: after a ForeignReduction, the ledger adopts broker truth."""
        if broker_net == 0:
            self._owned.pop(symbol, None)
        else:
            self._owned[symbol] = broker_net
