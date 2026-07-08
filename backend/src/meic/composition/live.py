"""Live-mode composition root — EC-RSK-04.

"Paper and live are structurally separate wirings, not a flag." This root
binds the BrokerGateway to the real TastytradeAdapter and MarketDataFeed to
the DXLinkAdapter; the SimulatedBroker is never imported or constructed here.
Same services, same event log — the domain is unaware of the mode.

Every real fill flows through the adapter's allocation reconciler (STP-02d);
per_side selection stays config-gated. Construction is I/O-free; connect()
opens the sessions (the issuer guard refuses a non-cert token before any
network call when is_test=True).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from meic.adapters.dxlink.adapter import DXLinkAdapter
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.adapters.tastytrade.adapter import TastytradeAdapter
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
class LiveComposition:
    clock: object
    ticks: TickTable
    provider_secret: str
    refresh_token: str
    is_test: bool = True  # cert unless explicitly wired to production
    stop_basis: StopBasis = StopBasis.TOTAL_CREDIT
    events: list = field(default_factory=list)
    state_store: object = None  # inject a SqliteStateStore for durable state (REC-07)

    def __post_init__(self) -> None:
        # BrokerGateway -> live adapter (SimulatedBroker is NOT constructed here)
        self.broker = TastytradeAdapter(self.provider_secret, self.refresh_token, is_test=self.is_test)
        self.feed = DXLinkAdapter(session=None, clock=self.clock)  # session set on connect()
        self.state = PersistentState(self.state_store or InMemoryStateStore())
        self.state.trading_mode = "live"  # DAY-05
        self.alerts = _NullAlerts()
        self.execute = ExecuteEntryAttempt(self.broker, self.clock, self.events, self.ticks,
                                           stop_basis=self.stop_basis)
        self.protect = ProtectPosition(self.broker, self.clock, self.alerts, self.events, self.ticks)
        self.recover = RecoverLong(self.broker, self.events, self.ticks)
        self.close = CloseEntry(self.broker, self.events)
        self.day = RunTradingDay(self.clock, self.state, self.execute, self.events,
                                 on_filled=self._on_filled)

    async def connect(self, account_number: str | None = None) -> None:
        await self.broker.connect(account_number)
        self.feed._session = self.broker._session  # share the authenticated session

    async def _on_filled(self, entry_id: str, condor, stop=None) -> None:
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
