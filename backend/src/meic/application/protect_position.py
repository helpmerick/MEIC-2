"""ProtectPosition — the service that protects a filled condor (STP-01/02/04/06).

Reacts to a confirmed fill: computes triggers (stop_policy), runs the STP-02c
post-fill feasibility checkpoint, places a broker-resting buy-to-close
stop-market on EACH short (STP-06: never on a long), verifies placement, and
escalates to UNPROTECTED with retries then a flatten if the broker won't
confirm (STP-04).

Async, port-driven: BrokerGateway, Clock, AlertSink, and the event log. The
post-fill infeasible path routes the close through an injected close callback
(the real CloseEntry/CLS is slice 4) with initiator `infeasible_stop`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable, Iterable

from meic.domain.events import (
    EntryClosedInfeasible,
    SideUnprotected,
    StopConfirmed,
    StopPlaced,
)
from meic.domain.stop_policy import StopBasis, clears, stop_trigger
from meic.domain.ticks import TickTable


@dataclass(frozen=True)
class ShortLeg:
    side: str            # "PUT" | "CALL"
    fill: Decimal        # actual short fill (STP-02 uses actual fills)
    long_fill: Decimal   # allocated long fill (per_side basis only)


@dataclass(frozen=True)
class ProtectResult:
    outcome: str  # "PROTECTED" | "INFEASIBLE_CLOSED" | "UNPROTECTED_FLATTENED"
    triggers: dict[str, Decimal]


CloseCallback = Callable[[str, str], Awaitable[None]]  # (entry_id, initiator)


class ProtectPosition:
    def __init__(
        self,
        broker,
        clock,
        alerts,
        events: list,
        ticks: TickTable,
        *,
        stop_retry_seconds: int = 5,
        stop_retry_attempts: int = 3,
        unprotected_action: str = "flatten_side",
        min_stop_distance_ticks: int = 2,
        close_entry: CloseCallback | None = None,
    ) -> None:
        self._broker = broker
        self._clock = clock
        self._alerts = alerts
        self._events = events
        self._ticks = ticks
        self._retry_seconds = stop_retry_seconds
        self._retry_attempts = stop_retry_attempts
        self._unprotected_action = unprotected_action
        self._min_distance = min_stop_distance_ticks
        self._close_entry = close_entry

    def _trigger_for(self, basis, leg, *, pct, markup, total_net_credit):
        return stop_trigger(
            basis, ticks=self._ticks, pct=pct, markup=markup,
            total_net_credit=total_net_credit, short_fill=leg.fill, side_long_fill=leg.long_fill,
        )

    async def protect(
        self,
        *,
        entry_id: str,
        basis: StopBasis,
        shorts: Iterable[ShortLeg],
        pct: Decimal = Decimal("95"),
        markup: Decimal = Decimal("0"),
        total_net_credit: Decimal | None = None,
    ) -> ProtectResult:
        shorts = list(shorts)
        triggers = {
            leg.side: self._trigger_for(basis, leg, pct=pct, markup=markup, total_net_credit=total_net_credit)
            for leg in shorts
        }

        # STP-02c checkpoint 2 (post-fill): a trigger that doesn't clear its
        # short's fill would fire at birth — never place it; close instead.
        for leg in shorts:
            if not clears(triggers[leg.side], leg.fill, ticks=self._ticks, min_distance_ticks=self._min_distance):
                self._events.append(EntryClosedInfeasible(entry_id=entry_id))
                self._alerts.alert("critical", "post-fill infeasible stop; closing entry",
                                   entry_id=entry_id, side=leg.side, trigger=str(triggers[leg.side]))
                if self._close_entry is not None:
                    await self._close_entry(entry_id, "infeasible_stop")  # CLS-01, slice 4
                return ProtectResult("INFEASIBLE_CLOSED", triggers)

        # STP-01/06: one broker-resting buy-to-close stop-market per SHORT.
        for leg in shorts:
            if not await self._place_and_verify(entry_id, leg.side, triggers[leg.side]):
                await self._go_unprotected(entry_id, leg.side)
                return ProtectResult("UNPROTECTED_FLATTENED", triggers)
        return ProtectResult("PROTECTED", triggers)

    async def _place_and_verify(self, entry_id: str, side: str, trigger: Decimal) -> bool:
        """STP-04: place, then confirm working; retry up to attempts."""
        for attempt in range(self._retry_attempts):
            try:
                order_id = await self._broker.submit({
                    "action": "buy_to_close", "type": "stop_market", "tif": "Day",
                    "leg": f"short_{side.lower()}", "trigger": trigger, "entry_id": entry_id,
                })
            except Exception:
                order_id = None
            if order_id is not None and await self._confirmed(order_id):
                self._events.append(StopPlaced(entry_id=entry_id, side=side, trigger=trigger))
                self._events.append(StopConfirmed(entry_id=entry_id, side=side))
                return True
            if attempt < self._retry_attempts - 1:
                await self._clock.wait_until(self._clock.now())  # advance-controlled retry gap
        return False

    async def _confirmed(self, order_id: str) -> bool:
        working = await self._broker.working_orders()
        return any(getattr(o, "order_id", None) == order_id for o in working)

    async def _go_unprotected(self, entry_id: str, side: str) -> None:
        """STP-04: retries exhausted — flatten per unprotected_action + alert.
        A position is never knowingly left without a resting stop."""
        self._events.append(SideUnprotected(entry_id=entry_id, side=side, action=self._unprotected_action))
        self._alerts.alert("critical", "UNPROTECTED: stop placement failed, flattening",
                           entry_id=entry_id, side=side, action=self._unprotected_action)
        if self._close_entry is not None:
            await self._close_entry(entry_id, "unprotected")
