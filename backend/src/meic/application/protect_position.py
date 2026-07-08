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
from datetime import date
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

from .order_intent import OrderIntent, protective_stop, working_order_qty


@dataclass(frozen=True)
class ShortLeg:
    """A filled short, carrying the identity needed to protect it.

    A stop must name the *instrument* it closes. Identity is mandatory, and is
    exactly one of `strike` (our own selection — the ACL resolves symbology,
    doc 05 §121) or `symbol` (broker-sourced, already resolved). Before v1.44
    this carried only `side`, so no stop could be translated into a real order.
    """

    side: str            # "PUT" | "CALL"
    fill: Decimal        # actual short fill (STP-02 uses actual fills)
    long_fill: Decimal   # allocated long fill (per_side basis only)
    strike: Decimal | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        if self.side not in ("PUT", "CALL"):
            raise ValueError(f"side must be PUT or CALL, got {self.side!r}")
        if (self.strike is None) == (self.symbol is None):
            raise ValueError(
                f"short {self.side} needs exactly one of `strike` or `symbol` — a stop "
                "with no instrument identity cannot be placed")

    @property
    def right(self) -> str:
        """OCC right: PUT -> P, CALL -> C."""
        return "P" if self.side == "PUT" else "C"


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
        contracts: int = 1,
        expiration: date | None = None,
        underlying: str = "SPXW",
    ) -> ProtectResult:
        """`contracts` (v1.44, ENT-04) sizes each stop at the position it
        protects — a 2-contract condor gets 2-contract stops."""
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
            intent = protective_stop(
                entry_id=entry_id, right=leg.right, contracts=contracts,
                trigger=triggers[leg.side], strike=leg.strike, symbol=leg.symbol,
                underlying=underlying, expiration=expiration,
                idempotency_key=f"stop:{entry_id}:{leg.side}",  # ORD-04
            )
            placed = await self._place_and_verify(
                entry_id, leg.side, triggers[leg.side], intent, contracts)
            if placed is not True:
                await self._go_unprotected(entry_id, leg.side, naked=placed or None)
                return ProtectResult("UNPROTECTED_FLATTENED", triggers)
        return ProtectResult("PROTECTED", triggers)

    async def _place_and_verify(self, entry_id: str, side: str, trigger: Decimal,
                                intent: OrderIntent, contracts: int) -> bool | int:
        """STP-04: place, then confirm working; retry up to attempts.

        Returns True on confirmation, False if it never confirmed, or the NAKED
        QUANTITY if it confirmed at the wrong size (STP-01 quantity invariant,
        v1.45). A short-sized stop is not retried and never silently resized — it
        is an UNPROTECTED condition immediately.
        """
        for attempt in range(self._retry_attempts):
            try:
                order_id = await self._broker.submit(intent)
            except Exception:
                order_id = None
            if order_id is not None:
                qty = await self._confirmed_qty(order_id)
                if qty == contracts:
                    self._events.append(StopPlaced(entry_id=entry_id, side=side, trigger=trigger))
                    self._events.append(StopConfirmed(entry_id=entry_id, side=side))
                    return True
                if qty is not None:
                    # STP-01: it IS working, at the wrong size. Retrying would rest a
                    # second stop beside it; resizing it ourselves is forbidden.
                    return contracts - qty      # the naked quantity
            if attempt < self._retry_attempts - 1:
                await self._clock.wait_until(self._clock.now())  # advance-controlled retry gap
        return False

    async def _confirmed_qty(self, order_id: str) -> int | None:
        """The working stop's quantity, or None if it isn't working at all.
        An unreadable quantity is 'unknown', which is NOT 'confirmed'."""
        for o in await self._broker.working_orders():
            if getattr(o, "order_id", None) == order_id:
                return working_order_qty(o)
        return None

    async def _go_unprotected(self, entry_id: str, side: str, *, naked: int | None = None) -> None:
        """STP-04: retries exhausted, or the stop confirmed at the wrong size
        (STP-01) — flatten per unprotected_action + alert. A position is never
        knowingly left without a resting stop that covers all of it."""
        self._events.append(SideUnprotected(entry_id=entry_id, side=side, action=self._unprotected_action))
        if naked is not None:
            self._alerts.alert(
                "critical", f"UNPROTECTED: stop quantity mismatch, {naked} contract(s) naked",
                entry_id=entry_id, side=side, action=self._unprotected_action, naked_quantity=str(naked))
        else:
            self._alerts.alert("critical", "UNPROTECTED: stop placement failed, flattening",
                               entry_id=entry_id, side=side, action=self._unprotected_action)
        if self._close_entry is not None:
            await self._close_entry(entry_id, "unprotected")
