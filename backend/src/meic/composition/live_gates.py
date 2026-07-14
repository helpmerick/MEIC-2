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

from meic.application.entry_gates import GateSnapshot
from meic.application.market_calendar import ET, is_market_open

# DAY-03: `ET` is re-exported from market_calendar (the ONE shared ET zone) --
# every other module that did `from meic.composition.live_gates import ET`
# keeps working unchanged; this file must never declare its own ZoneInfo.


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

    @classmethod
    def for_live(cls, **kw) -> "LiveMarketGates":
        """The LIVE boot seam (DAY-01a, v1.60/v1.61): the live gates boot with a
        COMPUTED exchange calendar loaded (nyse_holidays.py, ≥ a decade in the
        live wiring), and an EMPTY calendar at boot is a CONSTRUCTION ERROR,
        never an open market — the dataclass defaults (empty frozensets) once
        made every market holiday look like an open day. server.py's live
        wiring constructs through THIS classmethod; direct `LiveMarketGates(...)`
        construction stays available to paper/test call-sites that legitimately
        pass explicit sets for controlled scenarios."""
        if not kw.get("holidays"):
            raise ValueError(
                "DAY-01a: live gates constructed with no holiday data — an empty "
                "exchange calendar at boot is a construction error, not an open "
                "market. Compute it (nyse_holidays.holidays_near), never hand-feed it.")
        if not kw.get("half_days"):
            raise ValueError(
                "DAY-01a: live gates constructed with no half-day data — the exchange "
                "calendar (including half days, DAY-01) must be computed and loaded "
                "at boot (nyse_holidays.half_days_near).")
        return cls(**kw)

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
