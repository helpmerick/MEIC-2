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


class _Position:  # mimics the SDK CurrentPosition: attributes only, NO `.get`
    def __init__(self, symbol: str, quantity: int, quantity_direction: str) -> None:
        self.symbol = symbol
        self.quantity = quantity
        self.quantity_direction = quantity_direction  # "Long" | "Short"


class LiveShapedBroker:
    def __init__(self, clock, *, fill_delay: float = 3.0, buying_power: Decimal = Decimal("100000")) -> None:
        self._clock = clock
        self._delay = fill_delay
        self._bp = buying_power
        self._n = 1000
        self._orders: dict[str, dict] = {}
        self._positions: list[_Position] = []
        self.submits: list[tuple[str, str]] = []   # (order_id, order_type) in submit order

    def _sym(self, intent, leg) -> str:
        return leg.symbol or occ_symbol(intent.underlying, intent.expiration, leg.right, leg.strike)

    def _legs(self, intent) -> list[_Leg]:
        return [_Leg(self._sym(intent, l), l.action, intent.contracts) for l in intent.legs]

    def _is_filled(self, rec: dict) -> bool:
        if rec["cancelled"]:
            return False
        if rec.get("stop_filled"):   # a stop the test's market traded through (see fill_stop)
            return True
        # otherwise only entry LIMIT orders fill (after the latency); stops rest
        if rec["kind"] != "limit":
            return False
        return (self._clock.now() - rec["t"]).total_seconds() >= self._delay

    def fill_stop(self, order_id) -> None:
        """Mark a resting stop FILLED (the market traded through its trigger)
        — the exact condition the live stop-fill catch-up (EC-STP-06) must
        detect. From then on the order appears in `fills_since` as an SDK
        `_Order` and disappears from `working_orders`, just like the real
        broker's view of the 2026-07-10 C7565 fill."""
        self._orders[str(order_id)]["stop_filled"] = True

    def set_positions(self, positions: list[tuple[str, int, str]]) -> None:
        """Install broker-truth positions as SDK-shaped objects (attributes
        only, no `.get`): [(symbol, quantity, "Long"|"Short"), ...]. The live
        TastytradeAdapter's `positions()` returns exactly this shape — a
        consumer that assumes dicts crashes only in production, the incident
        class this harness exists to reproduce."""
        self._positions = [_Position(s, q, d) for s, q, d in positions]

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
        # resting stops (and any still-working, uncancelled limit) show as WORKING;
        # a stop marked filled via fill_stop() is no longer working
        out = []
        for oid, r in self._orders.items():
            if r["cancelled"] or self._is_filled(r):
                continue
            if r["kind"] in ("stop_market", "limit"):
                out.append(_Order(oid, "Live", self._legs(r["intent"])))
        return out

    async def buying_power(self) -> Decimal:
        return self._bp

    async def server_time(self) -> datetime:
        return datetime.now(timezone.utc)

    async def positions(self):
        return list(self._positions)
