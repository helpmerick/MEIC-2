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

    def can_afford(self, margin: Decimal) -> bool:
        """ENT-03 BP gate against simulated capital (SIM-04)."""
        return self.buying_power >= margin

    def to_dict(self) -> dict[str, str]:
        """REC-07: the ledger is durable state — serialize it for restart."""
        return {"cash": str(self.cash), "margin_held": str(self._margin_held)}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "SimLedger":
        return cls(cash=Decimal(d["cash"]), _margin_held=Decimal(d["margin_held"]))


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
        events: list | None = None,
    ) -> None:
        self._ids = itertools.count(1)
        self._orders: dict[str, SimOrder] = {}
        self.ledger = ledger or SimLedger()
        self._tick = tick
        self._through = fill_through_ticks
        self._slippage = stop_slippage_ticks
        self._fee = fee_per_leg
        self.events: list = events if events is not None else []  # shared with the pipeline (SIM-05)
        self._market = None  # provider(intent) -> (natural, mid, is_credit); paper's real feed

    def set_market(self, provider) -> None:
        """Bind the market snapshot the fill model evaluates against — the REAL
        DXLink feed in paper mode (SIM-01), a scripted snapshot in tests."""
        self._market = provider

    # --- SIM-02: try to fill a limit order against a market snapshot ----------
    def try_fill_limit(self, order_id: str, *, natural: Decimal, mid: Decimal, is_credit: bool) -> bool:
        o = self._orders[order_id]
        raw = o.intent.get("net_credit", o.intent.get("price"))  # entries carry net_credit; legs carry price
        limit = Decimal(str(raw))
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
        from meic.domain.events import ShortStopped

        o = self._orders[order_id]
        trigger = Decimal(str(o.intent["trigger"]))
        if o.status == "WORKING" and stop_triggered(mark, trigger):
            price = stop_fill_price(trigger, tick=self._tick, slippage_ticks=self._slippage)
            o.status, o.fill_price = "FILLED", price
            self._settle(o, signed=-price, legs=1)  # buy-to-close a short
            # SIM-05: a paper stop fill emits the SAME event a live fill would,
            # routing the side into SIDE_STOPPED -> LEX through the normal pipeline.
            side = "PUT" if "put" in o.intent.get("leg", "").lower() else "CALL"
            self.events.append(ShortStopped(
                entry_id=o.intent.get("entry_id", ""), side=side, fill=price,
                slippage=price - trigger, initiator="resting_stop"))
            return price
        return None

    def _settle(self, o: SimOrder, *, signed: Decimal, legs: int) -> None:
        self.ledger.post_fill(signed * 100, fee=self._fee * legs)
        o.intent["mode"] = self.PAPER  # SIM-05 stamp

    # --- BrokerGateway surface ------------------------------------------------
    async def submit(self, order: dict) -> str:
        oid = f"SIM-{next(self._ids)}"
        self._orders[oid] = SimOrder(order_id=oid, intent=dict(order), status="WORKING")

        # SIM-04: an opening order may carry its worst-case margin. The ENT-03
        # BP gate strains against simulated capital exactly as live — if the
        # ledger can't afford it the entry is skipped (rejected_bp), never filled.
        margin_req = order.get("margin_req")
        if margin_req is not None:
            margin_req = Decimal(str(margin_req))
            if not self.ledger.can_afford(margin_req):
                self._orders[oid].status = "REJECTED"
                self.events.append({"type": "order_rejected", "reason": "rejected_bp",
                                    "order_id": oid, "entry_id": order.get("entry_id", "")})
                return oid

        # A limit order fills immediately iff the current real market trades
        # through it (SIM-02); stops rest until a mark reaches the trigger.
        if self._market is not None and order.get("type") in ("limit", "marketable_limit"):
            snap = self._market(order)
            if snap is not None:
                natural, mid, is_credit = snap
                if self.try_fill_limit(oid, natural=natural, mid=mid, is_credit=is_credit) \
                        and margin_req is not None:
                    self.ledger.hold_margin(margin_req)  # consumed until the entry closes
        return oid

    def tick_marks(self, marks: dict[str, Decimal], *, entry_id: str | None = None) -> list[tuple[str, Decimal]]:
        """Feed current short marks; fill any resting stop whose trigger is hit
        (SIM-03). Scope to one entry_id when only one side should move. Returns
        the (order_id, fill_price) of stops that filled."""
        filled = []
        for oid, o in list(self._orders.items()):
            if o.status == "WORKING" and o.intent.get("type") == "stop_market":
                if entry_id is not None and o.intent.get("entry_id") != entry_id:
                    continue
                leg = o.intent.get("leg", "")
                if leg in marks:
                    price = self.try_fill_stop(oid, mark=marks[leg])
                    if price is not None:
                        filled.append((oid, price))
        return filled

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
