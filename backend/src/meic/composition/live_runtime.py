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

from decimal import Decimal

from meic.application.entry_gates import GateSnapshot, RiskSnapshot, clock_drift_blocks_entry
from meic.application.execute_entry import Condor, ExecuteEntryAttempt, StopParams
from meic.application.reconcile_boot import entries_blocked_by_reconcile
from meic.domain.events import DayArmed, DayCompleted, EntrySkipped
from meic.domain.projection import fold
from meic.domain.risk import OrderCap
from meic.domain.stop_policy import StopBasis

from .live_selection import SelectionConfig

# (condor, skip_reason) — exactly one is non-None
Selector = Callable[..., Awaitable[tuple[Condor | None, str | None]]]
GatesProvider = Callable[[], Awaitable[GateSnapshot]]
Warmup = Callable[[datetime], Awaitable[None]]


@dataclass(frozen=True)
class ScheduledRow:
    """One row of the standing schedule, fired at `when` with its OWN settings."""

    when: datetime
    entry: object | None = None      # domain.schedule.ResolvedEntry (None => globals)

    @property
    def selection(self) -> SelectionConfig | None:
        return None if self.entry is None else SelectionConfig.for_entry(self.entry)

    @property
    def stop(self) -> StopParams | None:
        if self.entry is None:
            return None
        return StopParams(basis=StopBasis(self.entry.stop_basis),
                          pct=Decimal(self.entry.stop_loss_pct),
                          markup=self.entry.stop_rebate_markup)


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
    max_day_risk: Decimal | None = None       # RSK-04; mandatory before live (doc 06 §169)
    order_cap: OrderCap | None = None         # RSK-08
    buying_power: Callable[[], Decimal] | None = None   # ENT-03 BP vs this condor's margin

    def __post_init__(self) -> None:
        self._worst_case: dict[str, Decimal] = {}   # entry_id -> its structural worst case

    def _skip(self, day: str, n: int, reason: str) -> None:
        self.comp.events.append(EntrySkipped(date=day, entry_number=n, reason=reason))

    def _risk(self) -> RiskSnapshot:
        """RSK-08 + RSK-04 + ENT-03 BP. Only entries still OPEN count toward
        exposure. `new_worst_case` is a placeholder — ExecuteEntryAttempt re-prices
        it from the condor, so the runtime cannot under-report its own entry."""
        open_ids = {eid for eid, e in fold(self.comp.events).entries.items() if not e.close_initiator}
        return RiskSnapshot(
            new_worst_case=Decimal("0"),
            open_worst_cases=tuple(wc for eid, wc in self._worst_case.items() if eid in open_ids),
            max_day_risk=self.max_day_risk,
            order_cap_allows_entry=(self.order_cap is None
                                    or self.order_cap.allow(exit_priority=False)),
            buying_power=self.buying_power() if self.buying_power is not None else None,
        )

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

    async def run_day(self, day: str, schedule: list[ScheduledRow] | list[datetime]) -> int:
        """Fire each composed entry at its wall-clock time. Returns the fill count.

        Accepts ScheduledRows (each carrying its own ENT-04 settings) or bare
        datetimes, which mean "use the global config for every row".
        """
        comp = self.comp
        rows = [r if isinstance(r, ScheduledRow) else ScheduledRow(r) for r in schedule]
        rows.sort(key=lambda r: r.when)
        comp.events.append(DayArmed(date=day, entry_count=len(rows)))
        cap = self.max_entries_per_day if self.max_entries_per_day is not None else len(rows)
        filled = 0

        for n, row in enumerate(rows, start=1):
            when = row.when
            # ENT-08: warm up ahead of the entry; never let it delay the clock.
            if self.warmup is not None:
                await comp.clock.wait_until(when - timedelta(seconds=self.warmup_lead_seconds))
                await self.warmup(when)

            await comp.clock.wait_until(when)

            reason = self._blocked_reason(filled, cap)
            if reason is not None:
                self._skip(day, n, reason)
                continue

            # Selection runs against THIS ROW's target premium / wing width / credit
            # floors, and the Condor it returns carries the row's `contracts`.
            condor, skip = await self.selector(when, n, row.selection)
            if condor is None:
                self._skip(day, n, skip or "selection_unavailable")
                continue

            gates = await self.market_gates()
            outcome = await comp.execute.attempt(
                day=day, scheduled=when, condor=condor, gates=gates,
                risk=self._risk(), stop=row.stop)
            if outcome.status == "FILLED":
                filled += 1
                # ExecuteEntryAttempt keys its events off condor.entry_number, so we
                # must too. Keying off the loop index instead would silently drop
                # entries out of the RSK-04 total whenever the two disagreed.
                entry_id = f"{day}#{condor.entry_number}"
                # RSK-04: on the books — counts against every entry that follows.
                self._worst_case[entry_id] = ExecuteEntryAttempt.worst_case(condor)
                await comp._on_filled(entry_id, condor, row.stop)  # STP-01 protect hand-off

        comp.events.append(DayCompleted(date=day))
        return filled
