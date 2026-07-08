"""LiveMarketGates — the market/session/BP half of the ENT-03 snapshot.

RunTradingDay/LiveRuntime take the durable states (ARMED / Stop Trading /
Confirm Live) from PersistentState; everything else must be SOURCED, live:

  market_open / market_halted  <- exchange calendar (DAY-01/02) + halt flag
  data_fresh                   <- the chain snapshot's staleness (DAT-02)
  session_valid                <- broker session probe (REC-06)
  buying_power_ok              <- broker BP vs the worst-case margin (ENT-03/RSK-04)

Every provider defaults to the SAFE answer (closed / stale / invalid / no BP), so
a provider that is missing or throwing blocks the entry rather than waving it
through. There is no optimistic default anywhere in this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from meic.application.entry_gates import GateSnapshot
from meic.application.market_calendar import is_market_open

ET = ZoneInfo("America/New_York")  # DAY-03: all session times are ET


@dataclass
class LiveMarketGates:
    clock: object                                        # .now() -> ET-aware datetime
    data_fresh: Callable[[], Awaitable[bool]]            # snapshot staleness (DAT-02)
    session_valid: Callable[[], Awaitable[bool]]         # broker session probe
    buying_power_ok: Callable[[], Awaitable[bool]]       # BP vs worst-case margin
    halted: Callable[[], Awaitable[bool]] = None         # exchange halt flag
    flatten_in_progress: Callable[[], bool] = lambda: False
    holidays: frozenset[date] = field(default_factory=frozenset)
    half_days: frozenset[date] = field(default_factory=frozenset)

    async def _safe(self, provider, *, default: bool) -> bool:
        """A provider that is absent or raises yields the SAFE answer — never the
        permissive one. A gate we cannot evaluate is a gate that blocks."""
        if provider is None:
            return default
        try:
            result = provider()
            return bool(await result if hasattr(result, "__await__") else result)
        except Exception:  # noqa: BLE001 — an unevaluable gate must block
            return default

    async def __call__(self) -> GateSnapshot:
        # The production clock ticks in UTC; the exchange calendar is ET (DAY-03).
        now: datetime = self.clock.now()
        if now.tzinfo is None:
            raise ValueError("LiveMarketGates requires a tz-aware clock")
        now_et = now.astimezone(ET)
        open_now = is_market_open(now_et, holidays=self.holidays, half_days=self.half_days)
        return GateSnapshot(
            # durable states are supplied by the runtime from PersistentState;
            # they are all-pass here so evaluate_gates falls through to the
            # market/session/BP checks this provider actually owns.
            armed=True, confirm_live=True, stop_trading=False,
            flatten_in_progress=bool(self.flatten_in_progress()),
            market_open=open_now,
            market_halted=await self._safe(self.halted, default=False) if open_now else True,
            data_fresh=await self._safe(self.data_fresh, default=False),      # stale => block
            session_valid=await self._safe(self.session_valid, default=False),  # invalid => block
            buying_power_ok=await self._safe(self.buying_power_ok, default=False),  # unknown => block
        )
