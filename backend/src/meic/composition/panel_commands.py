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

from meic.application.manual_close import FLATTEN_CONFIRMATION, ManualClose
from meic.application.market_calendar import trading_day_str
from meic.composition.close_assembly import DEFAULT_CLOSE_PRICE, assemble_close_inputs
from meic.domain import tpf as tpf_domain
from meic.domain import tpt as tpt_domain
from meic.domain.events import CloseIncomplete, ShortStopped
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
        # CLS-03 (2026-07-11 wiring): the ratified cancel path for a WORKING
        # (pre-fill) entry — built lazily, see `_cancel_service`.
        self._manual_close: ManualClose | None = None
        # DCY-01 (2026-07-14): "never while a Flatten All is executing" needs a
        # REAL live signal -- there was none anywhere in the composition before
        # this (even `LiveMarketGates.flatten_in_progress` defaults to a dead
        # `lambda: False`). True only for the duration of this instance's own
        # `flatten()` call below; the decay watcher's wiring reads it live via
        # the `flatten_in_progress` property.
        self._flatten_in_progress = False

    @property
    def flatten_in_progress(self) -> bool:
        return self._flatten_in_progress

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
                   put_floor=None, call_floor=None, blackout_ack: bool = False) -> dict:
        if self._manual is None:
            return {"result": "unavailable", "reason": "manual entry not wired (ENT-09)"}
        return await self._manual.fire(press_id=press_id, entry_number=entry_number,
                                       row=row, confirmed=confirmed,
                                       put_floor=put_floor, call_floor=call_floor,
                                       blackout_ack=blackout_ack)

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
        return trading_day_str(self._comp.clock.now())

    async def close(self, entry_id: str) -> dict:
        """Close one entry via CLS (manual). No-op if it is already closed —
        projection-based idempotency (a double-click yields exactly one close)."""
        return await self.close_as(entry_id, "manual")

    async def close_as(self, entry_id: str, initiator: str) -> dict:
        """The ONE close path every PanelCommands caller uses (CLS-02):
        `manual` (the operator's Close button, UC-14), `take_profit` (the TPF
        floor monitor) and `take_profit_target` (the TPT target monitor, both
        TPF-04/TPT — "no close logic of its own") all route through here.
        No-op if the entry is already closed — projection-based idempotency.

        CLS-06 (v1.85): this is ALSO the close-boundary — nothing below ever
        escapes as an unstructured 500 (the 2026-07-20 tester incident,
        PROPOSAL-CLOSE-500-2026-07-21.md). A PRE-ACTION failure (nothing sent
        to the broker yet) reports `close_failed`/`pre_submit`; a MID-
        SEQUENCE failure (some sides already closed and journaled) reports
        `close_partial`/`in_flight` naming `sides_closed`/`sides_remaining` —
        NEVER as a generic failure, which would present a half-closed entry
        as an untouched one (the real money-risk this rule fixes). Reporting
        only: CLS-01's procedure and CLS-02's single implementation are
        unchanged.
        """
        day = fold(self._comp.events)
        e = day.entries.get(entry_id)
        if e is None:
            return {"result": "unknown_entry"}
        open_sides = _open_sides(e)
        if e.close_initiator or not open_sides:
            return {"result": "already_closed"}

        # CLS-03 (UC-14/TC-CLS-02, 2026-07-11 wiring): "If the entry's opening
        # order is still PENDING/WORKING (nothing filled), 'close' means cancel
        # the entry order ... — no close orders are placed for unfilled legs."
        # Nothing filled == no broker-reported legs AND no credit collected.
        # The working order id comes from the ladder's registry (it is
        # journaled nowhere else); a pre-fill entry whose ladder already ended
        # has nothing working and nothing to cancel.
        if not e.legs and not e.net_credit:
            # CLS-06: the cancel path is covered by the SAME never-a-raw-
            # 500 rule — the ratified result taxonomy has no separate
            # `cancel_failed`; a raise here is just as pre-action as an
            # unassembled close (nothing was sent for an unfilled entry's
            # cancel either), so it reports through the identical
            # close_failed/pre_submit shape.
            try:
                return await self._cancel_working_entry(entry_id)
            except Exception as exc:
                return {"result": "close_failed", "stage": "pre_submit", "reason": str(exc)}

        # ORD-09: close the instruments the BROKER said it filled. This used to
        # build LiveLeg(f"{entry_id}:{s}", ...) — a placeholder that paper ignored
        # and cert would have rejected, because no such instrument exists. If no
        # legs were recorded we refuse rather than invent a symbol. Assembly
        # (legs + per-side stop ids) is shared with the STP-04 AUTO-FLATTEN
        # hook (composition/close_assembly.py) — one assembly, not two.
        #
        # CLS-06: a raise here is PRE-ACTION — `assemble_close_inputs` only
        # READS broker/journal state (working_orders, LegBook); it never
        # submits an order. Nothing was sent, the position is unchanged.
        try:
            inputs = await assemble_close_inputs(
                self._comp.events, self._comp.broker, entry_id, open_sides=set(open_sides))
        except Exception as exc:
            return {"result": "close_failed", "stage": "pre_submit", "reason": str(exc)}
        if inputs is None:
            return {"result": "legs_unrecorded", "entry_id": entry_id}
        legs, stop_ids = inputs

        # CLS-06: a prior `close_partial` left `incomplete_close_legs` naming
        # EXACTLY the legs that never closed. Restrict THIS call to those
        # symbols only — `open_sides` above is SIDE-scoped and would also
        # re-include an already-closed short on the same side (e.g. the CALL
        # short's replace landed clean while only the CALL long's sell
        # raised); a side-blind retry would re-submit a buy-to-close on a
        # short already flat at the broker, opening an unintended long — the
        # CLS-01 double-fill class of risk this exists to prevent. `stop_ids`
        # is left as the FRESH broker-truth correlation just assembled above
        # — a remainder short still STILL_RESTING re-correlates to its real
        # (possibly re-replaced) resting stop.
        if e.incomplete_close_legs:
            remaining_symbols = {leg[0] for leg in e.incomplete_close_legs}
            legs = [leg for leg in legs if leg.symbol in remaining_symbols]

        # CLS-06: `progress` lets `CloseEntry` report exactly which legs
        # completed before a mid-sequence raise — the partial truth this
        # boundary needs to tell `close_partial` apart from `close_failed`.
        # `events_before` marks the journal high-water so the classifier can
        # read which ShortStopped events THIS call appended (the ORD-08a
        # FILLED-race detector below).
        events_before = len(self._comp.events)
        progress: list = []
        try:
            await self._comp.close.close(
                entry_id, initiator, resting_stop_ids=stop_ids,
                live_legs=legs, close_price=DEFAULT_CLOSE_PRICE, progress=progress)
        except Exception as exc:
            if not progress:
                # CLS-06 / C2 nuance (flagged, not resolved here — out of
                # scope for this ruling): a LOST-ACK submit (the broker
                # actually received the order but the response never made it
                # back) would make "position unchanged" optimistic. ORD-04
                # idempotency keys bound that damage — a resubmit of the same
                # intent cannot double-fill — but this boundary cannot yet
                # tell "genuinely nothing sent" apart from "sent, ack lost".
                return {"result": "close_failed", "stage": "pre_submit", "reason": str(exc)}
            # CLS-01(3)/LEX-01: a short that raced to FILLED during its
            # replace is a genuine stop-out — CloseEntry journals ShortStopped
            # and structurally EXCLUDES that side's sibling long from its own
            # longs loop (LEX, triggered by the ShortStopped event, owns that
            # long's sale — not CLS). That long was therefore never this
            # close's to attempt: counting it as `remaining` would misname
            # LEX's leg in the remainder AND strip the raced side out of
            # `sides_closed` even though its short genuinely closed. The
            # ShortStopped events appended during THIS call identify exactly
            # those raced sides.
            raced_sides = {ev.side for ev in list(self._comp.events)[events_before:]
                           if isinstance(ev, ShortStopped) and ev.entry_id == entry_id}
            cls_legs = [leg for leg in legs
                        if not (leg.role == "long" and leg.side in raced_sides)]
            by_side: dict[str, list] = {}
            for leg in cls_legs:
                by_side.setdefault(leg.side, []).append(leg)
            remaining = [leg for leg in cls_legs if leg not in progress]
            sides_closed = sorted(side for side, side_legs in by_side.items()
                                  if all(leg in progress for leg in side_legs))
            sides_remaining = sorted({leg.side for leg in remaining})
            if not remaining:
                # CLS-06 edge: EVERY leg's exit completed at the broker and
                # the call STILL raised — the only step left after the last
                # leg is journaling `EntryClosed` (CLS-04), so this is a
                # journal-write failure, not a broker one. A no-op
                # CloseIncomplete(remaining=()) is deliberately NOT journaled
                # (appending to the same failing journal would likely raise
                # again, and there is no remainder for a second click to
                # close). AWARENESS: until this resolves, the entry displays
                # non-CLOSED (no EntryClosed landed) even though the book is
                # flat at the broker — the critical alert below is the
                # operator's signal to check the journal (REC-01).
                # ("CLS-owned exits", not "close complete": a raced side's
                # long is LEX's job (LEX-01), so the book may legitimately
                # still hold that long — everything CLS itself owed is done.)
                try:
                    self._comp.alerts.alert(
                        "critical", "CLS-06 CLS-owned exits complete at broker but the "
                        "closing journal write failed — journal/operator attention "
                        "(REC-01); any raced side's long is LEX's (LEX-01)",
                        entry_id=entry_id, error=str(exc))
                except Exception:
                    # Alerts are best-effort by construction everywhere else
                    # (a _NullAlerts default, sinks swapped in live) — a
                    # failing sink must not turn a structured answer into a
                    # 500.
                    pass
                return {"result": "close_partial", "stage": "in_flight", "reason": str(exc),
                        "sides_closed": sides_closed, "sides_remaining": []}
            # CLS-06: the never-500 rule holds even when the JOURNAL itself is
            # down — if the mid-sequence raise WAS a journal failure (the
            # journal-first event log raises on append), this append raises
            # again, and letting it escape would leak a raw 500 in exactly the
            # systemic-failure case where the operator most needs the
            # structured answer. The response still reports the partial truth
            # (extended with the journal failure); ORD-04 idempotency keys
            # bound any damage from the re-click the toast asks for.
            reason = str(exc)
            try:
                self._comp.events.append(CloseIncomplete(
                    entry_id=entry_id, initiator=initiator, sides_closed=tuple(sides_closed),
                    remaining=tuple((leg.symbol, leg.side, leg.role, leg.signed_qty) for leg in remaining),
                    reason=str(exc), at=self._comp.clock.now().isoformat()))
            except Exception as exc2:
                reason = (f"{exc}; AND the CloseIncomplete journal write failed: "
                          f"{exc2} — REC-01 attention")
            # RSK-06: a partial close is critical — the book may carry an
            # unintended naked/mismatched leg until the operator clicks Close
            # again. Best-effort like every other alert sink use (see above).
            try:
                self._comp.alerts.alert(
                    "critical", "CLS-06 close PARTIAL — remainder open, click Close again",
                    entry_id=entry_id, sides_remaining=sides_remaining, error=str(exc))
            except Exception:
                pass
            # CLS-06: TPF/TPT are deliberately NOT cleared here — the entry
            # still carries open exposure, and the monitors must keep routing
            # any further trigger through this SAME idempotent close_as path
            # for the remainder.
            return {"result": "close_partial", "stage": "in_flight", "reason": reason,
                    "sides_closed": sides_closed, "sides_remaining": sides_remaining}
        self._clear_tpf(entry_id)
        self._clear_tpt(entry_id)
        return {"result": "closed", "initiator": initiator}

    def _cancel_service(self) -> ManualClose:
        """CLS-03/CLS-02: `ManualClose.cancel_working` is the ONE ratified
        cancel path for a WORKING entry (its post-cancel fills re-check is the
        2026-07-11 race guard). Built LAZILY so it binds the composition's
        LIVE alert sink and broker: server.py installs `comp.alerts` (and
        wraps `comp.broker`) AFTER this command object is constructed."""
        if self._manual_close is None:
            comp = self._comp
            self._manual_close = ManualClose(comp.close, comp.broker, comp.state,
                                             alerts=comp.alerts, events=comp.events)
        return self._manual_close

    async def _cancel_working_entry(self, entry_id: str) -> dict:
        """CLS-03: cancel a WORKING (pre-fill) entry's opening order. The
        stand-down flag is raised BEFORE the broker cancel goes out, so the
        reprice ladder (execute_entry) never replaces — on the live adapter's
        cancel-then-submit fallback, never RESUBMITS — the order out from
        under the cancel. A `race_detected` outcome (the entry filled in the
        click→cancel window) alerts critically and journals a
        ReconciliationMismatch inside `cancel_working` itself; it is returned
        distinctly so the UI never renders it as a clean cancel."""
        registry = getattr(self._comp, "working_entries", None)
        order_id = registry.get(entry_id) if registry is not None else None
        if order_id is None:
            return {"result": "no_working_order", "entry_id": entry_id}
        registry.request_cancel(entry_id)
        res = await self._cancel_service().cancel_working(entry_id, order_id)
        # cancel_working cleared the TPF floor; TPT targets are panel-side.
        self._clear_tpt(entry_id)
        return {"result": res.result, "initiator": res.initiator, "entry_id": entry_id}

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
        # DCY-01: flagged for the duration of this call so the decay watcher's
        # gate can see "a Flatten All is executing" and never race a buyback
        # against it. Cleared in `finally` so a raised exception mid-flatten
        # never leaves the flag stuck true.
        self._flatten_in_progress = True
        try:
            day = fold(self._comp.events)
            closed = []
            for entry_id, e in day.entries.items():
                if not e.close_initiator and _open_sides(e):
                    await self.close(entry_id)
                    closed.append(entry_id)
            return {"result": "flattened", "entries": closed}
        finally:
            self._flatten_in_progress = False

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
