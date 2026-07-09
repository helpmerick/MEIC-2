"""A LIVE-shaped async fake broker — for exercising the real order/fill/stop path.

The paper SimulatedBroker/FakeBroker fill SYNCHRONOUSLY and return dicts / `.order_id`
objects; the real TastytradeAdapter returns SDK order OBJECTS and fills with LATENCY.
Three live-only bugs shipped on 2026-07-09 because nothing tested the live shapes:
  1. `_filled` did `.get(...)` on a fill record (SDK objects have no `.get`)
  2. `_confirmed_qty` matched working orders on `.order_id` (SDK orders use `.id`)
  3. the reprice ladder had a ZERO gap in live, repricing a filling order into a
     duplicate (margin_check_failed) and never placing stops

This harness reproduces all three conditions: SDK object shapes (`.id`/`.status`/
`.legs`), fill LATENCY (an entry limit fills `fill_delay` after submit, by the clock),
and a REJECT when an already-filled order is repriced (the broker's real behaviour).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from meic.adapters.tastytrade.occ import occ_symbol
from meic.domain.events import FilledLeg


class _Leg:  # mimics an SDK order leg
    def __init__(self, symbol: str, action: str, quantity: int) -> None:
        self.symbol = symbol
        self.action = action
        self.quantity = quantity


class _Order:  # mimics the SDK PlacedOrder: `.id`, `.status`, `.legs` (NO `.get`, NO `.order_id`)
    def __init__(self, id: str, status: str, legs: list[_Leg]) -> None:
        self.id = id
        self.status = status
        self.legs = legs


class LiveShapedBroker:
    def __init__(self, clock, *, fill_delay: float = 3.0, buying_power: Decimal = Decimal("100000")) -> None:
        self._clock = clock
        self._delay = fill_delay
        self._bp = buying_power
        self._n = 1000
        self._orders: dict[str, dict] = {}
        self.submits: list[tuple[str, str]] = []   # (order_id, order_type) in submit order

    def _sym(self, intent, leg) -> str:
        return leg.symbol or occ_symbol(intent.underlying, intent.expiration, leg.right, leg.strike)

    def _legs(self, intent) -> list[_Leg]:
        return [_Leg(self._sym(intent, l), l.action, intent.contracts) for l in intent.legs]

    def _is_filled(self, rec: dict) -> bool:
        # only entry LIMIT orders fill (after the latency); stops rest, never "fill"
        if rec["kind"] != "limit" or rec["cancelled"]:
            return False
        return (self._clock.now() - rec["t"]).total_seconds() >= self._delay

    async def submit(self, intent) -> str:
        oid = str(self._n)
        self._n += 1
        self._orders[oid] = {"intent": intent, "t": self._clock.now(),
                             "kind": intent.order_type, "cancelled": False}
        self.submits.append((oid, intent.order_type))
        return oid

    async def replace(self, oid, intent) -> str:
        rec = self._orders.get(str(oid))
        if rec is not None and self._is_filled(rec):
            # the real broker rejects the duplicate: the fill already used the margin
            raise RuntimeError("margin_check_failed: cannot reprice an already-filled order")
        if rec is not None:
            rec["cancelled"] = True
        return await self.submit(intent)

    async def cancel(self, oid) -> dict:
        rec = self._orders.get(str(oid))
        if rec is not None:
            rec["cancelled"] = True
        return {"result": "cancelled"}

    async def fills_since(self, cursor):
        return [_Order(oid, "Filled", self._legs(r["intent"]))
                for oid, r in self._orders.items() if self._is_filled(r)]

    async def fill_legs(self, order_id):
        rec = self._orders.get(str(order_id))
        if rec is None:
            return ()
        intent = rec["intent"]
        return tuple(
            FilledLeg(symbol=self._sym(intent, l), right=l.right,
                      role="short" if "sell_to_open" in l.action else "long",
                      qty=intent.contracts, price=None)
            for l in intent.legs
        )

    async def working_orders(self):
        # resting stops (and any still-working, uncancelled limit) show as WORKING
        out = []
        for oid, r in self._orders.items():
            if r["cancelled"]:
                continue
            if r["kind"] == "stop_market" or (r["kind"] == "limit" and not self._is_filled(r)):
                out.append(_Order(oid, "Live", self._legs(r["intent"])))
        return out

    async def buying_power(self) -> Decimal:
        return self._bp

    async def server_time(self) -> datetime:
        return datetime.now(timezone.utc)

    async def positions(self):
        return []
