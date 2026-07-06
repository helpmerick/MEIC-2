"""Quote staleness — DAT-02 (pure).

A quote is stale if older than max_quote_age_ms. Decisions (strike selection,
ladders, floors) must never run on stale data — the domain sees a
StampedQuote and asks; the adapter owns the stamping so the core never sees a
stale quote without knowing it (doc 05 Market Data context).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class StampedQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    stamped_at: datetime  # when the tick was received (staleness clock)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def crossed(self) -> bool:
        return self.bid > self.ask

    def is_stale(self, now: datetime, max_age_ms: int) -> bool:
        return (now - self.stamped_at) > timedelta(milliseconds=max_age_ms)

    def usable(self, now: datetime, max_age_ms: int) -> bool:
        """Fresh AND not crossed — the minimum for any pricing decision."""
        return not self.is_stale(now, max_age_ms) and not self.crossed
