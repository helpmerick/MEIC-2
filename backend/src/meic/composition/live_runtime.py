"""LiveRuntime — the wall-clock entry cadence for a live trading day.

RunTradingDay owns the offline cadence but takes a pre-built Condor per entry.
Live selection must happen AT FIRE TIME from fresh chain data, and a live day
needs three things the offline scheduler does not:

  * ENT-08 warm-up at T-`warmup_lead_seconds` (renew session, resubscribe,
    prime the chain) — and it NEVER delays the entry (the clock does not slip).
  * Two extra blocks before the ENT-03 chain: an unresolved reconciliation
    mismatch (REC-02 -> RSK-03) and clock drift beyond tolerance (RSK-07).
  * A selector that returns a Condor from the live chain, or a skip reason.

Safety by construction: `selector` and `market_gates` have NO defaults. There is
no optimistic fallback that could fire an entry on stale or absent data — if the
operator has not wired real selection and real gates, the runtime cannot trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from meic.application.entry_gates import GateSnapshot, clock_drift_blocks_entry
from meic.application.execute_entry import Condor
from meic.application.reconcile_boot import entries_blocked_by_reconcile
from meic.domain.events import DayArmed, DayCompleted, EntrySkipped

# (condor, skip_reason) — exactly one is non-None
Selector = Callable[[datetime, int], Awaitable[tuple[Condor | None, str | None]]]
GatesProvider = Callable[[], Awaitable[GateSnapshot]]
Warmup = Callable[[datetime], Awaitable[None]]


@dataclass
class LiveRuntime:
    comp: object                      # LiveComposition
    selector: Selector                # REQUIRED — no default: cannot trade without it
    market_gates: GatesProvider       # REQUIRED — no optimistic gate defaults
    warmup: Warmup | None = None
    max_entries_per_day: int | None = None
    warmup_lead_seconds: float = 60.0
    max_clock_drift_ms: float = 250.0
    measure_drift_ms: Callable[[], float] = lambda: 0.0

    def _skip(self, day: str, n: int, reason: str) -> None:
        self.comp.events.append(EntrySkipped(date=day, entry_number=n, reason=reason))

    def _blocked_reason(self, filled: int, cap: int) -> str | None:
        """Blocks evaluated before the ENT-03 chain, in precedence order."""
        state = self.comp.state
        if not state.entries_enabled():                       # ENT-01a/01b, RSK-01
            return state.blocking_state() or "disabled"
        if entries_blocked_by_reconcile(self.comp.events):    # REC-02 -> RSK-03
            return "reconcile_pending"
        if clock_drift_blocks_entry(drift_ms=self.measure_drift_ms(),
                                    max_drift_ms=self.max_clock_drift_ms):  # RSK-07
            return "clock_drift"
        if filled >= cap:                                     # ENT-05
            return "max_entries"
        return None

    async def run_day(self, day: str, entry_times: list[datetime]) -> int:
        """Fire each composed entry at its wall-clock time. Returns the fill count."""
        comp = self.comp
        comp.events.append(DayArmed(date=day, entry_count=len(entry_times)))
        cap = self.max_entries_per_day if self.max_entries_per_day is not None else len(entry_times)
        filled = 0

        for n, when in enumerate(sorted(entry_times), start=1):
            # ENT-08: warm up ahead of the entry; never let it delay the clock.
            if self.warmup is not None:
                await comp.clock.wait_until(when - timedelta(seconds=self.warmup_lead_seconds))
                await self.warmup(when)

            await comp.clock.wait_until(when)

            reason = self._blocked_reason(filled, cap)
            if reason is not None:
                self._skip(day, n, reason)
                continue

            condor, skip = await self.selector(when, n)
            if condor is None:
                self._skip(day, n, skip or "selection_unavailable")
                continue

            gates = await self.market_gates()
            outcome = await comp.execute.attempt(day=day, scheduled=when, condor=condor, gates=gates)
            if outcome.status == "FILLED":
                filled += 1
                await comp._on_filled(f"{day}#{n}", condor)  # STP-01 protect hand-off

        comp.events.append(DayCompleted(date=day))
        return filled
