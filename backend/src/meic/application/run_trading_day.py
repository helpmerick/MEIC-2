"""RunTradingDay — the day scheduler (DAY-04, ENT-01/01a/05).

Drives the standing schedule through the Clock port: at each composed entry
time it waits (FakeClock-able) and, iff entries are enabled (ARMED ∧ Stop
Trading OFF ∧ Confirm Live ON — the durable gate), delegates to
ExecuteEntryAttempt, which runs the full ENT-03 chain. Disabled ⇒ the entry is
skipped, its blocking state named. At most max_entries_per_day fills (ENT-05);
skipped entries are not retried (ENT-02). The day emits exactly one
DayArmed…DayCompleted bracket (DAY-04).

The reactive management (ProtectPosition on fill, RecoverLong on stop, decay,
TPF, EOD) is driven by process managers reacting to the same event log — this
service owns only the entry cadence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from meic.domain.events import DayArmed, DayCompleted, EntrySkipped
from meic.domain.projection import fold
from meic.domain.risk import OrderCap

from .execute_entry import Condor, ExecuteEntryAttempt
from .entry_gates import FilterSnapshot, GateSnapshot, RiskSnapshot
from .persistent_state import PersistentState


@dataclass(frozen=True)
class ScheduledEntry:
    when: datetime
    condor: Condor


class RunTradingDay:
    def __init__(
        self,
        clock,
        state: PersistentState,
        execute: ExecuteEntryAttempt,
        events: list,
        *,
        market_gates: GateSnapshot | None = None,
        max_entries_per_day: int | None = None,
        on_filled=None,
        max_day_risk: Decimal | None = None,
        order_cap: OrderCap | None = None,
        buying_power=None,   # () -> Decimal: SimLedger.buying_power in paper, broker in live
        # CAL-05 (v1.71): (day) -> NO-TRADE label | None, from the calendar tag
        # store (application/calendar_store.CalendarStore.label_for_day, itself
        # fail-open per CAL-07). None (every pre-v1.71 caller) means "no
        # calendar wired" -- entries are filtered exactly as before, never
        # blocked by an absent provider (CAL-07's polarity: absence => trade).
        calendar_label: Callable[[str], str | None] | None = None,
    ) -> None:
        self._clock = clock
        self._state = state
        self._execute = execute
        self._events = events
        self._on_filled = on_filled  # async (entry_id, condor) -> None: the
        #   ProtectPosition hand-off, injected by the composition root
        # market/session/bp portion of ENT-03 (all-pass default for offline days)
        self._market = market_gates or GateSnapshot(
            armed=True, confirm_live=True, stop_trading=False, flatten_in_progress=False,
            market_open=True, market_halted=False, data_fresh=True, session_valid=True,
            buying_power_ok=True)
        self._max = max_entries_per_day
        self._max_day_risk = max_day_risk          # RSK-04; mandatory before live (doc 06 §169)
        self._order_cap = order_cap                # RSK-08
        self._buying_power = buying_power          # ENT-03 BP, compared to THIS condor's margin
        self._calendar_label = calendar_label      # CAL-05
        self._worst_case: dict[str, Decimal] = {}  # entry_id -> its structural worst case

    def _risk(self, day: str) -> RiskSnapshot:
        """RSK-08 + RSK-04 inputs. Only entries still OPEN count toward exposure —
        a closed condor can no longer lose anything, so the event log (not a
        running total) decides who is still on the books.

        `new_worst_case` is a placeholder: ExecuteEntryAttempt re-prices it from
        the condor itself, so no caller can under-report the risk of its own entry.
        """
        open_ids = {eid for eid, e in fold(self._events).entries.items() if not e.close_initiator}
        return RiskSnapshot(
            new_worst_case=Decimal("0"),
            open_worst_cases=tuple(wc for eid, wc in self._worst_case.items() if eid in open_ids),
            max_day_risk=self._max_day_risk,
            order_cap_allows_entry=(self._order_cap is None
                                    or self._order_cap.allow(exit_priority=False)),
            buying_power=self._buying_power() if self._buying_power is not None else None,
        )

    def _gates(self) -> GateSnapshot:
        """ENT-03 snapshot: durable states from PersistentState, market/session
        from the injected provider (kept fresh by the health loop in live)."""
        return GateSnapshot(
            armed=self._state.armed,
            confirm_live=self._state.confirm_live,
            stop_trading=self._state.stop_trading,
            flatten_in_progress=self._market.flatten_in_progress,
            market_open=self._market.market_open,
            market_halted=self._market.market_halted,
            data_fresh=self._market.data_fresh,
            session_valid=self._market.session_valid,
            buying_power_ok=self._market.buying_power_ok,
        )

    def _filters(self, day: str) -> FilterSnapshot | None:
        """CAL-05: entries only -- this is the ONE place RunTradingDay reads
        the calendar tag store; no other call in this module (or anywhere
        stops/LEX/TPF/TPT/decay/EOD/reconcile run) ever does."""
        if self._calendar_label is None:
            return None
        return FilterSnapshot(date=day, blackout_label=self._calendar_label(day))

    async def run(self, day: str, schedule: list[ScheduledEntry]) -> int:
        """Run one trading day's entry cadence. Returns the fill count."""
        self._events.append(DayArmed(date=day, entry_count=len(schedule)))
        cap = self._max if self._max is not None else len(schedule)
        filled = 0
        for entry in sorted(schedule, key=lambda e: e.when):
            await self._clock.wait_until(entry.when)
            n = entry.condor.entry_number
            if not self._state.entries_enabled():  # ENT-01a durable gate
                self._events.append(EntrySkipped(date=day, entry_number=n,
                                                  reason=self._state.blocking_state() or "disabled"))
                continue
            if filled >= cap:  # ENT-05
                self._events.append(EntrySkipped(date=day, entry_number=n, reason="max_entries"))
                continue
            outcome = await self._execute.attempt(
                day=day, scheduled=entry.when, condor=entry.condor,
                gates=self._gates(), risk=self._risk(day), filters=self._filters(day))
            if outcome.status == "FILLED":
                filled += 1
                entry_id = f"{day}#{n}"
                # RSK-04: this entry is now on the books; its worst case counts
                # against max_day_risk for every entry that follows.
                self._worst_case[entry_id] = self._execute.worst_case(entry.condor)
                if self._on_filled is not None:  # ProtectPosition hand-off (STP-01)
                    await self._on_filled(entry_id, entry.condor)
        self._events.append(DayCompleted(date=day))
        return filled
