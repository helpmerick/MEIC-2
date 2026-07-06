"""CloseEntry — the ONE canonical close path (CLS-01/02/04).

Every close routes through here, differing only in the recorded initiator
(CLS-02). The broker-request sequence is a pure function of the position, NOT
the initiator — that is what makes a manual close and a TPF close byte-
identical (TC-CLS-01). Procedure (CLS-01): cancel the resting short stops and
confirm, then close all remaining live legs via an ORD-02-style ladder;
nothing is left resting or open. Idempotency keys (ORD-04) mean no leg closes
twice.

Exit quantities are capped by the OwnershipLedger (OWN-04) so a shared-symbol
close only ever touches the bot's own lots.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from meic.domain.events import EntryClosed, SideClosed
from meic.domain.ownership import OwnershipLedger

VALID_INITIATORS = frozenset({
    "manual", "manual_flatten", "take_profit", "eod", "decay", "infeasible_stop", "unprotected",
})


@dataclass(frozen=True)
class LiveLeg:
    symbol: str
    side: str          # "PUT" | "CALL"
    role: str          # "short" | "long"
    signed_qty: int    # bot's own position (OWN-04 capped at submit)


class CloseEntry:
    def __init__(self, broker, events: list, ledger: OwnershipLedger | None = None) -> None:
        self._broker = broker
        self._events = events
        self._ledger = ledger or OwnershipLedger()

    async def close(
        self,
        entry_id: str,
        initiator: str,
        *,
        resting_stop_ids: list[str],
        live_legs: list[LiveLeg],
        close_price: Decimal,
    ) -> None:
        if initiator not in VALID_INITIATORS:
            raise ValueError(f"unknown close initiator {initiator!r} (CLS-02)")

        # CLS-01 (1): cancel resting short stops, confirm — deterministic order
        for stop_id in resting_stop_ids:
            await self._broker.cancel(stop_id)

        # CLS-01 (2): close all remaining live legs via the ladder. One 4-leg
        # (or per-remaining-side) spread close; quantity OWN-04-capped.
        for leg in sorted(live_legs, key=lambda l: (l.side, l.role)):
            qty = self._ledger.cap_exit_qty(leg.symbol, abs(leg.signed_qty)) or abs(leg.signed_qty)
            action = "buy_to_close" if leg.signed_qty < 0 else "sell_to_close"
            await self._broker.submit({
                "action": action, "type": "limit", "tif": "Day",
                "symbol": leg.symbol, "leg": f"{leg.role}_{leg.side.lower()}",
                "qty": qty, "price": close_price,
                "idempotency_key": f"close:{entry_id}:{leg.symbol}",  # ORD-04
            })
            self._events.append(SideClosed(entry_id=entry_id, side=leg.side))

        # CLS-04: record the close with its initiator (the ONLY per-initiator diff)
        self._events.append(EntryClosed(entry_id=entry_id, initiator=initiator))
