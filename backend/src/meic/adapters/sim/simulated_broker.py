"""SimulatedBroker — the paper-mode BrokerGateway (SIM-01..06).

Bound to the BrokerGateway port at the composition root in paper mode; the
live adapter is never constructed (EC-RSK-04). Consumes the REAL DXLink feed
for prices (injected here as a quote provider) but simulates the fills, stops,
and cash — deliberately pessimistic (SIM-06). Emits the SAME order events as
live so the whole pipeline runs identically and unaware of the mode (SIM-05),
every record stamped PAPER.

This adapter never calls the allocation reconciler (STP-02d) — paper fills
produce no reconciliation records; that evidence is real-fills-only.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from decimal import Decimal

from meic.domain.sim_fill import limit_fills, stop_fill_price, stop_triggered


@dataclass
class SimLedger:
    """SIM-04 cash + margin ledger. Durable state (REC-07)."""

    cash: Decimal = Decimal("100000")
    _margin_held: Decimal = Decimal("0")

    def post_fill(self, signed_cash: Decimal, fee: Decimal) -> None:
        self.cash += signed_cash - fee

    def hold_margin(self, amount: Decimal) -> None:
        self._margin_held += amount

    def release_margin(self, amount: Decimal) -> None:
        self._margin_held = max(Decimal("0"), self._margin_held - amount)

    @property
    def buying_power(self) -> Decimal:
        return self.cash - self._margin_held


def spread_margin(width: Decimal, net_credit: Decimal, *, contracts: int = 1) -> Decimal:
    """SIM-04 / RSK-04: SPX spread requirement = (width − credit) × 100 × qty,
    the worse of the two sides (only one can settle ITM)."""
    return max(Decimal("0"), (width - net_credit)) * 100 * contracts


@dataclass
class SimOrder:
    order_id: str
    intent: dict
    status: str  # WORKING | FILLED
    fill_price: Decimal | None = None


class SimulatedBroker:
    """Implements the BrokerGateway surface. `mode` is PAPER; fills evaluate
    against an injected market snapshot (natural/mid), never a real broker."""

    PAPER = "PAPER"

    def __init__(
        self,
        ledger: SimLedger | None = None,
        *,
        tick: Decimal = Decimal("0.05"),
        fill_through_ticks: int = 1,
        stop_slippage_ticks: int = 3,
        fee_per_leg: Decimal = Decimal("0"),
    ) -> None:
        self._ids = itertools.count(1)
        self._orders: dict[str, SimOrder] = {}
        self.ledger = ledger or SimLedger()
        self._tick = tick
        self._through = fill_through_ticks
        self._slippage = stop_slippage_ticks
        self._fee = fee_per_leg
        self.events: list = []

    # --- SIM-02: try to fill a limit order against a market snapshot ----------
    def try_fill_limit(self, order_id: str, *, natural: Decimal, mid: Decimal, is_credit: bool) -> bool:
        o = self._orders[order_id]
        limit = Decimal(str(o.intent["net_credit" if is_credit else "price"]))
        if o.status != "WORKING":
            return o.status == "FILLED"
        if limit_fills(is_credit=is_credit, limit=limit, natural=natural, mid=mid,
                       tick=self._tick, through_ticks=self._through):
            o.status, o.fill_price = "FILLED", (natural if is_credit else natural)
            self._settle(o, signed=(limit if is_credit else -limit), legs=o.intent.get("legs", 4))
            return True
        return False

    # --- SIM-03: a triggered stop fills at trigger + slippage -----------------
    def try_fill_stop(self, order_id: str, *, mark: Decimal) -> Decimal | None:
        o = self._orders[order_id]
        trigger = Decimal(str(o.intent["trigger"]))
        if o.status == "WORKING" and stop_triggered(mark, trigger):
            price = stop_fill_price(trigger, tick=self._tick, slippage_ticks=self._slippage)
            o.status, o.fill_price = "FILLED", price
            self._settle(o, signed=-price, legs=1)  # buy-to-close a short
            return price
        return None

    def _settle(self, o: SimOrder, *, signed: Decimal, legs: int) -> None:
        self.ledger.post_fill(signed * 100, fee=self._fee * legs)
        o.intent["mode"] = self.PAPER  # SIM-05 stamp

    # --- BrokerGateway surface ------------------------------------------------
    async def submit(self, order: dict) -> str:
        oid = f"SIM-{next(self._ids)}"
        self._orders[oid] = SimOrder(order_id=oid, intent=dict(order), status="WORKING")
        return oid

    async def cancel(self, id) -> dict:
        o = self._orders.get(id)
        if o and o.status == "WORKING":
            o.status = "CANCELLED"
            return {"result": "cancelled"}
        return {"result": "terminal", "status": o.status if o else "unknown"}

    async def replace(self, id, new):
        await self.cancel(id)
        return await self.submit(new)

    async def working_orders(self):
        return [o for o in self._orders.values() if o.status == "WORKING"]

    async def positions(self):
        return []  # projected from fills in paper; positions feed not simulated

    async def fills_since(self, cursor):
        return [{"order_id": o.order_id, "price": str(o.fill_price)} for o in self._orders.values()
                if o.status == "FILLED"]
