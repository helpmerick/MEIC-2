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

import itertools
from decimal import Decimal

from meic.application.close_entry import LiveLeg
from meic.application.manual_close import FLATTEN_CONFIRMATION
from meic.application.leg_book import LegBook
from meic.domain.projection import EntryProjection, fold

_SIDES = ("PUT", "CALL")


def _open_sides(e: EntryProjection) -> list[str]:
    gone = set(e.sides_stopped) | set(e.sides_closed) | set(e.sides_expired)
    return [s for s in _SIDES if s not in gone]


class PanelCommands:
    def __init__(self, comp, manual_entry=None, preflight_checks=None) -> None:
        self._comp = comp
        self._manual = manual_entry               # ENT-09; None => the ▶ button is inert
        self.preflight_checks = preflight_checks  # UC-02 checklist providers
        self._presses = itertools.count(1)        # ENT-09: one id per PRESS

    # --- ENT-09 manual fire (UI-22) ---------------------------------------------
    def can_fire(self) -> bool:
        """UI-22: ▶ is enabled only while all three trade-enabling states permit
        entries. A wiring-less panel can never fire."""
        return self._manual is not None and self._manual.can_fire()

    def fire_preview(self, entry_number: int, row):
        if self._manual is None:
            raise RuntimeError("manual entry is not wired (ENT-09)")
        # The press id is minted here and echoed back on confirm, so the OK dialog
        # confirms the press it was opened for — a double-click cannot become two.
        #
        # It must be unique PER PRESS. Deriving it from the clock was wrong: two
        # separate presses inside one clock tick collided, and the operator's second,
        # entirely legitimate press came back `duplicate_press`. A counter cannot
        # collide, and unlike a timestamp it does not depend on clock resolution.
        press_id = f"fire:{entry_number}:{next(self._presses)}"
        return self._manual.preview(press_id, entry_number, row)

    async def fire(self, *, press_id: str, entry_number: int, row, confirmed: bool) -> dict:
        if self._manual is None:
            return {"result": "unavailable", "reason": "manual entry not wired (ENT-09)"}
        return await self._manual.fire(press_id=press_id, entry_number=entry_number,
                                       row=row, confirmed=confirmed)

    # --- ENT-11/UI-25 ad-hoc manual trade ---------------------------------------
    async def simulate(self, row) -> dict:
        """UI-25: read-only preview passthrough. A wiring-less panel can preview
        nothing, same as `fire`'s guard above."""
        if self._manual is None:
            return {"result": "unavailable", "reason": "manual entry not wired (ENT-09)"}
        return await self._manual.simulate(row)

    def day(self) -> str:
        """ENT-11(3): the day bucket a fire will stamp onto its entry_id/events —
        so the API layer can allocate the next 101+ ad-hoc number in the SAME
        bucket a fire is about to use. Falls back to the composition's own clock
        when manual entry isn't wired (nothing will actually fire in that case)."""
        if self._manual is not None:
            return self._manual.today()
        return self._comp.clock.now().date().isoformat()

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

        # ORD-09: close the instruments the BROKER said it filled. This used to
        # build LiveLeg(f"{entry_id}:{s}", ...) — a placeholder that paper ignored
        # and cert would have rejected, because no such instrument exists. If no
        # legs were recorded we refuse rather than invent a symbol.
        book = LegBook.from_events(self._comp.events)
        recorded = book.of(entry_id)
        if not recorded:
            return {"result": "legs_unrecorded", "entry_id": entry_id}
        legs = [
            LiveLeg(leg.symbol, leg.side, leg.role,
                    -leg.qty if leg.role == "short" else leg.qty)
            for leg in recorded if leg.side in open_sides
        ]
        stop_ids = [
            o.order_id for o in await self._comp.broker.working_orders()
            if getattr(getattr(o, "intent", None), "entry_id", None) == entry_id
            and getattr(getattr(o, "intent", None), "order_type", None) == "stop_market"
        ]
        await self._comp.close.close(
            entry_id, "manual", resting_stop_ids=stop_ids,
            live_legs=legs, close_price=Decimal("0.05"))
        self._clear_tpf(entry_id)
        return {"result": "closed", "initiator": "manual"}

    async def switch_mode(self, target: str, confirmation: str = "") -> dict:
        """UC-10/DAY-05: stage a paper/live switch. Requires a flat book (derived
        from the live projection + broker) and, for live, a typed LIVE. Staged
        changes are recorded to the durable log and take effect next day."""
        from meic.application.mode_switch import request_mode_switch
        from meic.domain.events import ModeSwitchStaged

        day = fold(self._comp.events)
        open_positions = sum(1 for e in day.entries.values()
                             if not e.close_initiator and _open_sides(e))
        working = len(await self._comp.broker.working_orders())
        result = request_mode_switch(
            target=target, current=self._comp.state.trading_mode,
            open_positions=open_positions, working_orders=working, confirmation=confirmation)
        if result.staged:
            self._comp.events.append(ModeSwitchStaged(target=target, effective=result.effective))
        return {"staged": result.staged, "target": result.target,
                "effective": result.effective, "reason": result.reason}

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
