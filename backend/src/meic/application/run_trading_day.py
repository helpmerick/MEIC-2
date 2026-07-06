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

from meic.domain.events import DayArmed, DayCompleted, EntrySkipped

from .execute_entry import Condor, ExecuteEntryAttempt
from .entry_gates import GateSnapshot
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
                day=day, scheduled=entry.when, condor=entry.condor, gates=self._gates())
            if outcome.status == "FILLED":
                filled += 1
                if self._on_filled is not None:  # ProtectPosition hand-off (STP-01)
                    await self._on_filled(f"{day}#{n}", entry.condor)
        self._events.append(DayCompleted(date=day))
        return filled
