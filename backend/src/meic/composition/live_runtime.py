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

import asyncio

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from decimal import Decimal

from meic.application.entry_gates import (
    FilterSnapshot, GateSnapshot, RiskSnapshot, clock_drift_blocks_entry,
)
from meic.application.execute_entry import Condor, ExecuteEntryAttempt, StopParams
from meic.application.reconcile_boot import entries_blocked_by_reconcile
from meic.domain.events import DayArmed, DayCompleted, EntrySkipped
from meic.domain.projection import fold
from meic.domain.risk import OrderCap
from meic.domain.stop_policy import StopBasis

from .live_selection import SelectionConfig

async def _maybe_await(provider):
    """Call a provider that may be sync or async. `None` means the rail is off."""
    import inspect

    if provider is None:
        return None
    value = provider()
    return await value if inspect.isawaitable(value) else value


def _alert_orphaned_failure(comp, context: str):
    """done-callback for a shielded attempt task: if it fails AFTER its caller was
    cancelled, nobody is left awaiting it — route the error to the alert sink
    (RSK-06), never /dev/null. (When the caller was NOT cancelled the exception
    also propagates through the shield await; the alert is the backstop for the
    orphaned case.)"""
    def _cb(task) -> None:
        if not task.cancelled() and task.exception() is not None:
            alerts = getattr(comp, "alerts", None)
            if alerts is not None:
                alerts.alert("critical",
                             f"orphaned entry attempt failed after cancel: {context}",
                             error=repr(task.exception()))
    return _cb


# (condor, skip_reason) — exactly one is non-None
Selector = Callable[..., Awaitable[tuple[Condor | None, str | None]]]
GatesProvider = Callable[[], Awaitable[GateSnapshot]]
# ENT-08 (operator ruling 2026-07-11): widened to carry the upcoming entry's
# number and its own SelectionConfig (row.selection) alongside `when` -- the
# real warm-up wiring (server.py `_wire_live_day`) needs BOTH to lock the
# STK-10 v1.55 baseline under the SAME (when, entry_number) key the fire will
# use (LiveCondorSelector.warm_baseline). `config` is `None` for a bare
# ScheduledRow (offline/global-settings callers).
Warmup = Callable[[datetime, int, "SelectionConfig | None"], Awaitable[None]]


@dataclass(frozen=True)
class ScheduledRow:
    """One row of the standing schedule, fired at `when` with its OWN settings."""

    when: datetime
    entry: object | None = None      # domain.schedule.ResolvedEntry (None => globals)
    # ENT-10(4) (v1.53, operator ruling): the row's DURABLE entry id, assigned
    # once at Save (schedule_service.save) and never reused — NOT its position
    # in any list. A mid-day restart filters the schedule down to the remaining
    # rows, and an ARMED mid-day edit (add/delete/reorder) can change that list's
    # shape at any time; without a stamped, position-independent id,
    # re-enumerating the filtered list would renumber row 3 as row 1 and collide
    # its entry_id with an already-filled entry (ORD-04 idempotency, RSK-04
    # book). A bare ScheduledRow with no `number` (the offline scheduler, or a
    # pre-v1.53 persisted schedule) falls back to loop position — the pre-v1.53
    # behaviour, kept only as a migration path.
    number: int | None = None

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
    # CAL-05 (v1.71): (day) -> NO-TRADE label | None, from the calendar tag
    # store. None (every pre-v1.71 wiring) means "no calendar wired" -- never
    # a block; CAL-07 rules absence as trade, so an unwired provider must
    # read the SAME way a wired-but-empty one does.
    calendar_label: Callable[[str], str | None] | None = None

    def __post_init__(self) -> None:
        self._worst_case: dict[str, Decimal] = {}   # entry_id -> its structural worst case

    def _skip(self, day: str, n: int, reason: str) -> None:
        self.comp.events.append(EntrySkipped(date=day, entry_number=n, reason=reason))

    async def _risk(self) -> RiskSnapshot:
        """RSK-08 + RSK-04 + ENT-03 BP. Only entries still OPEN count toward
        exposure. `new_worst_case` is a placeholder — ExecuteEntryAttempt re-prices
        it from the condor, so the runtime cannot under-report its own entry.

        Async because a live broker's buying power is an authenticated call. It is
        fetched HERE, immediately before the gate that uses it, rather than read
        from something a previous await happened to refresh.
        """
        open_ids = {eid for eid, e in fold(self.comp.events).entries.items() if not e.close_initiator}
        return RiskSnapshot(
            new_worst_case=Decimal("0"),
            open_worst_cases=tuple(wc for eid, wc in self._worst_case.items() if eid in open_ids),
            max_day_risk=self.max_day_risk,
            order_cap_allows_entry=(self.order_cap is None
                                    or self.order_cap.allow(exit_priority=False)),
            buying_power=await _maybe_await(self.buying_power),
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

        # ENT-10(4): a mid-day restart (or a mid-day ARMED edit) passes a FILTERED
        # schedule; rows carry their DURABLE ids so entry_ids never collide with
        # already-filled entries (ORD-04/RSK-04).
        for idx, row in enumerate(rows, start=1):
            n = row.number if row.number is not None else idx
            when = row.when
            # ENT-08: warm up ahead of the entry; never let it delay the clock.
            # `n` and `row.selection` (v1.55 hook, operator ruling 2026-07-11):
            # so the warm-up can lock the STK-10 baseline under the SAME
            # (when, entry_number) key the fire below will use.
            if self.warmup is not None:
                await comp.clock.wait_until(when - timedelta(seconds=self.warmup_lead_seconds))
                await self.warmup(when, n, row.selection)

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

            # ENT-10(3): disarm/stop cancels stop FUTURE entries instantly (the
            # waits are cancellable) but an IN-FLIGHT attempt is atomic — it runs
            # to its natural end (fill→protected, or cancelled-at-floor→skip).
            # Cancelling mid-ladder would orphan a live resting order at the
            # broker (re-review finding, 2026-07-09). The whole attempt — the
            # reprice ladder, fill recording, RSK-04 bookkeeping and the STP-01
            # protect hand-off — is ONE shielded unit; ensure_future keeps a
            # strong reference so the inner task is never GC'd.
            # CAL-05: entries only -- the ONE call in this codebase's live
            # scheduler that ever consults the calendar tag store. Management
            # (stops/LEX/TPF/TPT/decay/EOD/reconcile) is driven by process
            # managers elsewhere that never call this or `evaluate_filters`.
            filters = (FilterSnapshot(date=day, blackout_label=self.calendar_label(day))
                      if self.calendar_label is not None else None)

            async def _attempt_and_protect(when=when, row=row, condor=condor, gates=gates,
                                           filters=filters):
                nonlocal filled
                outcome = await comp.execute.attempt(
                    day=day, scheduled=when, condor=condor, gates=gates,
                    risk=await self._risk(), stop=row.stop, filters=filters)
                if outcome.status == "FILLED":
                    filled += 1
                    # ExecuteEntryAttempt keys its events off condor.entry_number, so
                    # we must too. Keying off the loop index instead would silently
                    # drop entries out of the RSK-04 total whenever they disagreed.
                    entry_id = f"{day}#{condor.entry_number}"
                    # RSK-04: on the books — counts against every entry that follows.
                    self._worst_case[entry_id] = ExecuteEntryAttempt.worst_case(condor)
                    # STP-02 (2026-07-09 fix): protect off the ACTUAL fill credit.
                    await comp._on_filled(entry_id, condor, row.stop,
                                         fill_credit=outcome.fill_credit)  # STP-01 hand-off
                return outcome

            attempt_task = asyncio.ensure_future(_attempt_and_protect())
            attempt_task.add_done_callback(
                _alert_orphaned_failure(comp, f"{day}#{condor.entry_number}"))
            await asyncio.shield(attempt_task)

        comp.events.append(DayCompleted(date=day))
        return filled
