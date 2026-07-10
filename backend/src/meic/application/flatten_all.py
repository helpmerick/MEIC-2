"""FlattenAll — RSK-01a one-shot flatten, OWN-07 boundary.

Closes the book and nothing more: iterate the bot's OWN open entries and close
each via the single canonical CloseEntry path (initiator `manual_flatten`),
entries processed concurrently (EC-API-02 exit-priority rate limiting lives in
the broker adapter, not here). It NEVER calls an account-wide flatten/close-all
API — there is deliberately no such method — and it only ever touches entries
in the supplied book, so FOREIGN positions (OWN-03) are untouched both because
they are not in the book and because CloseEntry caps every exit at the
ownership ledger (FOREIGN → 0).

This is purely a close orchestration: it does not block future entries
(Ash's rule, RSK-01a) — that is the operator's separate Stop Trading control.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

from meic.application.close_entry import CloseEntry, LiveLeg


@dataclass(frozen=True)
class OpenEntry:
    """One of the bot's own open entries, at recorded quantities (OWN-07).

    `resting_stop_ids` is keyed by side ("PUT"/"CALL") -> stop order id
    (CLS-01 v1.50): CloseEntry replaces EACH short's own stop, so it must know
    which id belongs to which side, not just a flat bag of ids to cancel."""
    entry_id: str
    live_legs: list[LiveLeg]
    close_price: Decimal
    resting_stop_ids: dict[str, str] = field(default_factory=dict)


class FlattenAll:
    def __init__(self, close_entry: CloseEntry) -> None:
        self._close = close_entry

    async def flatten(self, book: list[OpenEntry]) -> None:
        """Close every entry in the book concurrently via the canonical path.
        Nothing account-level is ever invoked; nothing outside the book is
        touched (OWN-07)."""
        await asyncio.gather(*(
            self._close.close(
                entry_id=e.entry_id,
                initiator="manual_flatten",  # RSK-01a: the one flatten initiator
                resting_stop_ids=e.resting_stop_ids,
                live_legs=e.live_legs,
                close_price=e.close_price,
            )
            for e in book
        ))
