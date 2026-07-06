"""FakeBroker — scripted BrokerGateway for the doc-04 harness.

Doc 04 harness requirements: scripted fills/partials/rejects/timeouts,
injected latencies, and crash/restart simulation. The FakeBroker is the
"outside world": a test (or a simulated bot restart) may discard the bot
instance and boot a new one against the SAME FakeBroker object — working
orders, positions and fills survive, exactly like a real broker across a
process crash (REC-02/03 scenarios).

Order payloads are opaque in Phase 1 (no domain types yet); everything is
keyed by broker order ids this fake issues.
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class Scripted:
    """One scripted reaction to the next submit()/cancel()/replace() call."""

    action: str  # "fill" | "partial" | "reject" | "timeout" | "work"
    latency_s: float = 0.0  # injected latency before the fake responds
    payload: dict[str, Any] = field(default_factory=dict)  # fill prices, reject reason, ...


@dataclass
class FakeOrder:
    order_id: str
    intent: Any
    status: str  # WORKING | FILLED | PARTIAL | REJECTED | CANCELLED | REPLACED
    fills: list[dict[str, Any]] = field(default_factory=list)


class FakeBroker:
    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self._orders: dict[str, FakeOrder] = {}
        self._positions: list[Any] = []
        self._fills: list[dict[str, Any]] = []
        self._submit_script: list[Scripted] = []
        self._cancel_script: list[Scripted] = []
        self._event_queues: list[asyncio.Queue[Any]] = []

    # ------------------------------------------------------------------ script
    def script_submit(self, *reactions: Scripted) -> None:
        """Queue reactions consumed in order by subsequent submit() calls."""
        self._submit_script.extend(reactions)

    def script_cancel(self, *reactions: Scripted) -> None:
        self._cancel_script.extend(reactions)

    def autofill(self, predicate) -> None:
        """Orders whose intent satisfies predicate(intent) fill immediately;
        everything else rests WORKING. Lets a scripted day fill entry orders
        while stops rest, without counting submits."""
        self._autofill = predicate

    def set_positions(self, positions: list[Any]) -> None:
        """Directly install broker-truth positions (reconcile scenarios)."""
        self._positions = list(positions)

    def emit(self, event: Any) -> None:
        """Push an account-stream event to every connected listener."""
        for q in self._event_queues:
            q.put_nowait(event)

    # ------------------------------------------------------------- BrokerGateway
    async def submit(self, order: Any) -> str:
        if self._submit_script:
            reaction = self._submit_script.pop(0)
        elif getattr(self, "_autofill", None) is not None and self._autofill(order):
            reaction = Scripted("fill", payload={"price": order.get("net_credit") if isinstance(order, dict) else None})
        else:
            reaction = Scripted("work")
        if reaction.latency_s:
            await asyncio.sleep(reaction.latency_s)
        if reaction.action == "timeout":
            raise TimeoutError("scripted broker timeout")
        order_id = f"FB-{next(self._ids)}"
        rec = FakeOrder(order_id=order_id, intent=order, status="WORKING")
        self._orders[order_id] = rec
        if reaction.action == "reject":
            rec.status = "REJECTED"
            self.emit({"type": "order_rejected", "order_id": order_id, **reaction.payload})
        elif reaction.action == "fill":
            self._record_fill(rec, reaction.payload, partial=False)
        elif reaction.action == "partial":
            self._record_fill(rec, reaction.payload, partial=True)
        return order_id

    async def cancel(self, id: str) -> dict[str, Any]:
        reaction = self._cancel_script.pop(0) if self._cancel_script else Scripted("work")
        if reaction.latency_s:
            await asyncio.sleep(reaction.latency_s)
        if reaction.action == "timeout":
            raise TimeoutError("scripted broker timeout")
        rec = self._orders.get(id)
        if rec is None:
            return {"result": "unknown_order"}
        if reaction.action == "reject" or rec.status in ("FILLED", "REJECTED", "CANCELLED"):
            return {"result": "terminal", "status": rec.status, **reaction.payload}
        rec.status = "CANCELLED"
        self.emit({"type": "order_cancelled", "order_id": id})
        return {"result": "cancelled"}

    async def replace(self, id: str, new: Any) -> str:
        old = self._orders.get(id)
        if old is None or old.status not in ("WORKING", "PARTIAL"):
            raise ValueError(f"cannot replace order {id!r} in state {old.status if old else 'missing'}")
        old.status = "REPLACED"
        return await self.submit(new)

    async def working_orders(self) -> list[FakeOrder]:
        return [o for o in self._orders.values() if o.status in ("WORKING", "PARTIAL")]

    async def positions(self) -> list[Any]:
        return list(self._positions)

    async def fills_since(self, cursor: int | None) -> list[dict[str, Any]]:
        start = 0 if cursor is None else cursor
        return self._fills[start:]

    def order_events(self) -> AsyncIterator[Any]:
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._event_queues.append(q)

        async def _stream() -> AsyncIterator[Any]:
            while True:
                yield await q.get()

        return _stream()

    # ---------------------------------------------------------------- internals
    def _record_fill(self, rec: FakeOrder, payload: dict[str, Any], *, partial: bool) -> None:
        rec.status = "PARTIAL" if partial else "FILLED"
        fill = {"order_id": rec.order_id, "cursor": len(self._fills) + 1, "partial": partial, **payload}
        rec.fills.append(fill)
        self._fills.append(fill)
        self.emit({"type": "order_partially_filled" if partial else "order_filled", **fill})
