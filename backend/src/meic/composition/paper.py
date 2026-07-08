"""Paper-mode composition root — SIM-01 / EC-RSK-04.

Binds the BrokerGateway port to the SimulatedBroker; the live adapter is never
constructed. Assembles the same services the live wiring uses — RunTradingDay,
ExecuteEntryAttempt, ProtectPosition, RecoverLong, CloseEntry — over one event
log and one PersistentState, so the whole pipeline runs identically and
unaware of the mode (SIM-05). Market data is injected (the REAL DXLink feed in
production; a scripted snapshot in tests).

This is the assembly the paper-mode E2E drives; "paper and live are
structurally separate wirings, not a flag" — this module constructs paper and
only paper.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.adapters.sim.simulated_broker import SimLedger, SimulatedBroker
from meic.application.close_entry import CloseEntry
from meic.application.execute_entry import ExecuteEntryAttempt
from meic.application.persistent_state import PersistentState
from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.application.recover_long import RecoverLong
from meic.application.run_trading_day import RunTradingDay
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickTable


class _NullAlerts:
    def alert(self, *a, **k):
        pass


@dataclass
class PaperComposition:
    clock: object
    ticks: TickTable
    starting_cash: Decimal = Decimal("100000")
    stop_basis: StopBasis = StopBasis.TOTAL_CREDIT
    events: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.broker = SimulatedBroker(SimLedger(cash=self.starting_cash), events=self.events)  # SIM-01
        self.state = PersistentState(InMemoryStateStore())
        self.state.trading_mode = "paper"  # DAY-05
        self.alerts = _NullAlerts()
        self.execute = ExecuteEntryAttempt(self.broker, self.clock, self.events, self.ticks,
                                           stop_basis=self.stop_basis)
        self.protect = ProtectPosition(self.broker, self.clock, self.alerts, self.events, self.ticks)
        self.recover = RecoverLong(self.broker, self.events, self.ticks)
        self.close = CloseEntry(self.broker, self.events)
        self.day = RunTradingDay(self.clock, self.state, self.execute, self.events,
                                 on_filled=self._on_filled)

    async def _on_filled(self, entry_id: str, condor, stop=None) -> None:
        """STP-01 hand-off: place the two resting stops for a filled condor.
        Shorts carry their fills; total_credit uses the entry's net credit."""
        await self.protect.protect(
            entry_id=entry_id,
            # doc 06 section 37: this row's stop settings, falling back to the globals.
            basis=(stop.basis if stop else self.stop_basis),
            pct=(stop.pct if stop else self.execute.default_stop.pct),
            markup=(stop.markup if stop else self.execute.default_stop.markup),
            shorts=[ShortLeg("PUT", condor.put_short_mid, Decimal("0.50"), strike=condor.put_short),
                    ShortLeg("CALL", condor.call_short_mid, Decimal("0.50"), strike=condor.call_short)],
            total_net_credit=condor.mid_credit,
            # ENT-04 (v1.44): each stop is sized to the condor it protects.
            contracts=condor.contracts,
            # 0DTE: the expiration IS today unless selection named one explicitly.
            expiration=condor.expiration or self.clock.now().date())

    def compose_and_arm(self, entry_times: list[str]) -> None:
        """Operator composes the standing schedule and arms (ENT-01a/01b)."""
        self.state.entry_schedule = [{"time": t} for t in entry_times]
        self.state.armed = True
        self.state.confirm_live = True
