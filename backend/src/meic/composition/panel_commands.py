"""Panel command orchestration — the operator's Close / Flatten actions.

Thin glue between the FastAPI control panel and the composition: it derives an
entry's still-open sides from the LIVE projection (so idempotency and "what is
open" come from broker/event truth, robust across the demo loop's resets),
then closes via the one canonical CloseEntry (initiator `manual`, UC-14) and
clears the entry's armed TPF floor. Flatten-all is gated on a typed FLATTEN
confirmation (TC-FLT-01); Close is instant (UI-16). The CLS-02 command contract
this mirrors is unit-tested in test_tc_cls_02.

Also carries the operator's TPF/TPT set/raise/lower/clear commands (TPF-06,
TPT-02): server-side gap validation (UI-03 "reject, never clamp" — TPF-02/
TPT-03) against the SAME profit% evaluator the bot-side monitor uses
(`domain.tpf.entry_profit_pct`), fed by an optional `profit_pct_provider`
callback the wiring supplies (server.py, off the live chain snapshot). With no
provider (e.g. paper, which has no live chain marks) the current profit% is
unknowable, and a set/raise/lower request is rejected rather than guessed.
"""
from __future__ import annotations

import itertools
from decimal import Decimal

from meic.application.manual_close import FLATTEN_CONFIRMATION
from meic.composition.close_assembly import DEFAULT_CLOSE_PRICE, assemble_close_inputs
from meic.domain import tpf as tpf_domain
from meic.domain import tpt as tpt_domain
from meic.domain.projection import EntryProjection, fold

_SIDES = ("PUT", "CALL")


def _open_sides(e: EntryProjection) -> list[str]:
    gone = set(e.sides_stopped) | set(e.sides_closed) | set(e.sides_expired)
    return [s for s in _SIDES if s not in gone]


class PanelCommands:
    def __init__(self, comp, manual_entry=None, preflight_checks=None,
                 profit_pct_provider=None, floor_candidates_provider=None,
                 drill_guidance_provider=None, default_drill_outage_seconds: float = 60.0) -> None:
        self._comp = comp
        self._manual = manual_entry               # ENT-09; None => the ▶ button is inert
        self.preflight_checks = preflight_checks  # UC-02 checklist providers
        self._presses = itertools.count(1)        # ENT-09: one id per PRESS
        # TPF-02/TPT-03: (entry_id) -> current profit% | None, off the SAME
        # evaluator the bot-side monitor uses. None (the default, e.g. paper)
        # means "unknown" -- floor/target set/raise/lower are rejected, never
        # guessed at.
        self._profit_pct_provider = profit_pct_provider
        # ENT-09b v1.57: (row) -> dict of per-side candidate strikes from the
        # entry's VALIDATED UNIVERSE (v1.55), for the ▶ dialog's floor
        # dropdowns. `None` (e.g. paper without a wired chain) means the
        # dialog cannot populate live strikes.
        self._floor_candidates_provider = floor_candidates_provider
        # UC-12 v1.56: () -> list[str], the drill dialog's advisory warnings
        # (near-trigger marks / an entry due soon). `None` -> no guidance
        # computed (e.g. paper, or a panel with no chain/schedule wired).
        self._drill_guidance_provider = drill_guidance_provider
        # UC-12 `drill_outage_seconds` (doc 06: range 10-300, default 60) --
        # used whenever a drill request doesn't specify its own duration.
        self._default_drill_outage_seconds = default_drill_outage_seconds

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

    async def fire(self, *, press_id: str, entry_number: int, row, confirmed: bool,
                   put_floor=None, call_floor=None) -> dict:
        if self._manual is None:
            return {"result": "unavailable", "reason": "manual entry not wired (ENT-09)"}
        return await self._manual.fire(press_id=press_id, entry_number=entry_number,
                                       row=row, confirmed=confirmed,
                                       put_floor=put_floor, call_floor=call_floor)

    def floor_candidates(self, row) -> dict:
        """ENT-09b v1.57: the ▶ dialog's floor dropdowns -- per-side candidate
        strikes from the entry's VALIDATED UNIVERSE (v1.55), each with its
        distance from spot and live mid, plus spot + the quote timestamp."""
        if self._floor_candidates_provider is None:
            return {"available": False}
        return {"available": True, **self._floor_candidates_provider(row)}

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
        return await self.close_as(entry_id, "manual")

    async def close_as(self, entry_id: str, initiator: str) -> dict:
        """The ONE close path every PanelCommands caller uses (CLS-02):
        `manual` (the operator's Close button, UC-14), `take_profit` (the TPF
        floor monitor) and `take_profit_target` (the TPT target monitor, both
        TPF-04/TPT — "no close logic of its own") all route through here.
        No-op if the entry is already closed — projection-based idempotency."""
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
        # legs were recorded we refuse rather than invent a symbol. Assembly
        # (legs + per-side stop ids) is shared with the STP-04 AUTO-FLATTEN
        # hook (composition/close_assembly.py) — one assembly, not two.
        inputs = await assemble_close_inputs(
            self._comp.events, self._comp.broker, entry_id, open_sides=set(open_sides))
        if inputs is None:
            return {"result": "legs_unrecorded", "entry_id": entry_id}
        legs, stop_ids = inputs
        await self._comp.close.close(
            entry_id, initiator, resting_stop_ids=stop_ids,
            live_legs=legs, close_price=DEFAULT_CLOSE_PRICE)
        self._clear_tpf(entry_id)
        self._clear_tpt(entry_id)
        return {"result": "closed", "initiator": initiator}

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

    async def run_outage_drill(self, outage_seconds: float | None = None,
                               confirmation: str = "") -> dict:
        """UC-12: run the stop-independence drill against the live broker and
        return the evidence for the panel to display.

        v1.56: in LIVE mode this requires a typed DRILL confirmation (operator
        present) — REFUSED (never run) without it, mirroring the LIVE
        mode-switch and FLATTEN typed-confirmation gates. Paper needs none.
        Guidance (near-trigger marks / an entry due within 10 minutes) is
        advisory only and never blocks the drill itself. `outage_seconds`
        `None` uses the wired `drill_outage_seconds` config default.
        """
        from meic.application.drills import drill_confirmation_ok, run_stop_independence_drill

        mode = self._comp.state.trading_mode
        if not drill_confirmation_ok(mode=mode, confirmation=confirmation):
            return {"result": "confirmation_required"}
        seconds = self._default_drill_outage_seconds if outage_seconds is None else outage_seconds
        guidance = self._drill_guidance_provider() if self._drill_guidance_provider else []
        ev = await run_stop_independence_drill(
            self._comp.broker, outage_seconds=seconds, mode=mode, guidance=guidance)
        return {"result": "ok", **ev.as_dict()}

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

    def _clear_tpt(self, entry_id: str) -> None:
        targets = dict(self._comp.state.tp_targets)
        if targets.pop(entry_id, None) is not None:
            self._comp.state.tp_targets = targets

    def _current_profit_pct(self, entry_id: str) -> Decimal | None:
        if self._profit_pct_provider is None:
            return None
        return self._profit_pct_provider(entry_id)

    def _known_entry(self, entry_id: str) -> bool:
        return entry_id in fold(self._comp.events).entries

    # --- TPF-06 / TPT-02 operator commands: set/raise/lower/clear -------------
    def set_tpf(self, entry_id: str, level: int) -> dict:
        """TPF-02/06: arm, raise or lower the floor. Server-side gap re-
        validation is authoritative (UI-15) — reject, never clamp."""
        if not self._known_entry(entry_id):
            return {"result": "unknown_entry"}
        profit = self._current_profit_pct(entry_id)
        if profit is None:
            return {"result": "rejected", "reason": "current profit% unavailable (stale/no data)"}
        if not tpf_domain.is_armable(level, profit):
            return {"result": "rejected",
                    "reason": f"too close - would trigger immediately (current profit {profit}%)"}
        floors = dict(self._comp.state.tpf_floors)
        floors[entry_id] = level
        self._comp.state.tpf_floors = floors
        return {"result": "armed", "entry_id": entry_id, "level": level}

    def clear_tpf(self, entry_id: str) -> dict:
        self._clear_tpf(entry_id)
        return {"result": "cleared", "entry_id": entry_id}

    def set_tpt(self, entry_id: str, level: int) -> dict:
        """TPT-02/03: set, raise or lower the target. TPT-03 (operator ruling
        1A): a target at or below current profit is REJECTED with "target
        already passed - current profit X%", never clamped, never treated as
        close-now."""
        if not self._known_entry(entry_id):
            return {"result": "unknown_entry"}
        profit = self._current_profit_pct(entry_id)
        if profit is None:
            return {"result": "rejected", "reason": "current profit% unavailable (stale/no data)"}
        if not tpt_domain.is_armable(level, profit):
            return {"result": "rejected",
                    "reason": f"target already passed - current profit {profit}%"}
        targets = dict(self._comp.state.tp_targets)
        targets[entry_id] = level
        self._comp.state.tp_targets = targets
        return {"result": "armed", "entry_id": entry_id, "level": level}

    def clear_tpt(self, entry_id: str) -> dict:
        self._clear_tpt(entry_id)
        return {"result": "cleared", "entry_id": entry_id}
