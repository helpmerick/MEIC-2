"""QuoteHub — the persistent, single-writer, generation-guarded marks table
(NFR-04). Pure logic; the adapter owns the actual sockets.

- **Single writer:** only the hub writes the marks table (apply_tick). The
  one-shot fetcher used at a decision moment returns data to its caller and
  NEVER writes here — two writers are structurally impossible.
- **Generation guard:** every live socket gets a monotonically increasing
  generation; every tick is tagged; a tick from any generation other than the
  current one is discarded on arrival, and marks never move backward in time.
  A zombie socket can never time-travel the table.
- **Decision moment while sick:** resolve_decision tries demand-reconnect
  (skip the backoff), then a scoped one-shot fetcher (marks untouched), then
  gives up safely — skip `data_unavailable`, freeze LEX, pause TPF/DCY, alert.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from .staleness import StampedQuote


class QuoteHub:
    def __init__(self) -> None:
        self._marks: dict[str, StampedQuote] = {}
        self._generation = 0
        self._healthy = True
        self.connection_count = 0  # happy path across a day = 1

    # --- socket lifecycle -----------------------------------------------------
    def open_generation(self) -> int:
        """A new live socket. In the happy path this is called exactly once per
        day; healing bumps it."""
        self._generation += 1
        self.connection_count += 1
        self._healthy = True
        return self._generation

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def healthy(self) -> bool:
        return self._healthy

    def mark_sick(self) -> None:
        self._healthy = False

    # --- the ONLY writer to the marks table -----------------------------------
    def apply_tick(self, quote: StampedQuote, *, generation: int) -> bool:
        """Write a tick iff it is from the current generation and not older than
        what we hold. Returns True if it landed."""
        if generation != self._generation:
            return False  # zombie generation — discard on arrival
        existing = self._marks.get(quote.symbol)
        if existing is not None and quote.stamped_at < existing.stamped_at:
            return False  # never move a mark backward in time
        self._marks[quote.symbol] = quote
        return True

    def mark(self, symbol: str) -> StampedQuote | None:
        return self._marks.get(symbol)


@dataclass(frozen=True)
class DecisionOutcome:
    result: str                 # "HEALED" | "FETCHER" | "GIVE_UP"
    data: object | None = None  # fetcher snapshot (returned to the caller only)
    reason: str | None = None   # "data_unavailable" on give-up


async def resolve_decision(
    hub: QuoteHub,
    *,
    demand_reconnect: Callable[[], Awaitable[bool]],
    scoped_fetch: Callable[[], Awaitable[object | None]],
) -> DecisionOutcome:
    """NFR-04 decision-moment resolution while the hub is sick. Never waits on
    the operator; everything is automatic and bounded."""
    if hub.healthy:
        return DecisionOutcome("HEALED")  # nothing to resolve
    if await demand_reconnect():          # (1) skip the backoff, one immediate attempt
        return DecisionOutcome("HEALED")
    snapshot = await scoped_fetch()       # (2) one-shot fetcher — marks untouched
    if snapshot is not None:
        return DecisionOutcome("FETCHER", data=snapshot)
    return DecisionOutcome("GIVE_UP", reason="data_unavailable")  # (3) give up safely
