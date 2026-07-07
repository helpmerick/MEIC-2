"""Panel command orchestration — the operator's Close / Flatten actions.

Thin glue between the FastAPI control panel and the composition: it derives an
entry's still-open sides from the LIVE projection (so idempotency and "what is
open" come from broker/event truth, robust across the demo loop's resets),
then closes via the one canonical CloseEntry (initiator `manual`, UC-14) and
clears the entry's armed TPF floor. Flatten-all is gated on a typed FLATTEN
confirmation (TC-FLT-01); Close is instant (UI-16). The CLS-02 command contract
this mirrors is unit-tested in test_tc_cls_02.
"""
from __future__ import annotations

from decimal import Decimal

from meic.application.close_entry import LiveLeg
from meic.application.manual_close import FLATTEN_CONFIRMATION
from meic.domain.projection import EntryProjection, fold

_SIDES = ("PUT", "CALL")


def _open_sides(e: EntryProjection) -> list[str]:
    gone = set(e.sides_stopped) | set(e.sides_closed) | set(e.sides_expired)
    return [s for s in _SIDES if s not in gone]


class PanelCommands:
    def __init__(self, comp) -> None:
        self._comp = comp

    async def close(self, entry_id: str) -> dict:
        """Close one entry via CLS (manual). No-op if it is already closed —
        projection-based idempotency (a double-click yields exactly one close)."""
        day = fold(self._comp.events)
        e = day.entries.get(entry_id)
        if e is None:
            return {"result": "unknown_entry"}
        open_sides = _open_sides(e)
        if e.close_initiator or not open_sides:
            return {"result": "already_closed"}

        legs = [LiveLeg(f"{entry_id}:{s}", s, "short", -1) for s in open_sides]
        stop_ids = [
            o.order_id for o in await self._comp.broker.working_orders()
            if getattr(o, "intent", {}).get("entry_id") == entry_id
            and getattr(o, "intent", {}).get("type") == "stop_market"
        ]
        await self._comp.close.close(
            entry_id, "manual", resting_stop_ids=stop_ids,
            live_legs=legs, close_price=Decimal("0.05"))
        self._clear_tpf(entry_id)
        return {"result": "closed", "initiator": "manual"}

    async def run_outage_drill(self, outage_seconds: float = 2.0) -> dict:
        """UC-12: run the stop-independence drill against the live broker and
        return the evidence for the panel to display."""
        from meic.application.drills import run_stop_independence_drill
        ev = await run_stop_independence_drill(self._comp.broker, outage_seconds=outage_seconds)
        return ev.as_dict()

    async def flatten(self, confirmation: str) -> dict:
        """RSK-01a: close every open entry — but only on a typed FLATTEN."""
        if confirmation != FLATTEN_CONFIRMATION:
            return {"result": "confirmation_required"}
        day = fold(self._comp.events)
        closed = []
        for entry_id, e in day.entries.items():
            if not e.close_initiator and _open_sides(e):
                await self.close(entry_id)
                closed.append(entry_id)
        return {"result": "flattened", "entries": closed}

    def _clear_tpf(self, entry_id: str) -> None:
        floors = dict(self._comp.state.tpf_floors)
        if floors.pop(entry_id, None) is not None:
            self._comp.state.tpf_floors = floors
