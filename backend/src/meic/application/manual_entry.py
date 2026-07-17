"""ManualEntry — ENT-09 (v1.44) manual fire, and the UI-22 confirmation dialog.

The operator presses ▶ on a schedule row and fires that entry on demand, outside
any scheduled window. The ONLY rule this bypasses is the ENT-02 window — which
exists to guard against STALE SCHEDULED INTENT, and a manual press is fresh intent
by definition. Everything else applies unreduced, because the fire goes through
the identical `ExecuteEntryAttempt.attempt()` as a scheduled entry:

    full ENT-03 gate chain, reconcile-block, clock drift, ENT-07 sequencing,
    RSK-08 order cap, and RSK-04 max exposure.

ENT-05 v1.81 (operator-ruled, user-blocked, RETIRED): there is no entry-COUNT
cap here or anywhere else — a real user was blocked firing a legitimate
manual entry because the old cap defaulted to the scheduled-row count and
manual fires counted against it. The day's entry volume is bounded only by
RSK-04 (dollars) and the Cboe order cap (RSK-08) above.

Three things are ManualEntry's own:

  * UI-22 confirmation. A simple OK dialog (operator-ratified: NOT typed), in
    BOTH paper and live. No confirmation, no order — and no attempt recorded.
  * The ▶ enablement rule. The button is live only while all three trade-enabling
    states permit entries; a press while any of them blocks is refused `blocked`
    before an attempt runs (TC-ENT-08 scenario 3).
  * Idempotency per press. A double-click produces exactly ONE attempt.

Recorded with initiator `manual_entry`, tagged like other manual actions in
reports (UC-08).
"""
from __future__ import annotations

import asyncio

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from meic.application.attempt_crash import alert_and_journal_crashed_attempt
from meic.application.market_calendar import trading_day_str
from meic.domain.events import EntrySkipped, ManualFireBlackoutAcknowledged
from meic.domain.walk import floor_inside_spot

from .schedule_service import effective_stop_pct_estimate, worst_case_estimate

MANUAL = "manual_entry"


async def _maybe_await(provider):
    """Call a provider that may be sync or async. `None` means the rail is off.
    Live's risk snapshot needs an authenticated buying-power call; paper's does not."""
    import inspect

    if provider is None:
        return None
    value = provider()
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class FirePreview:
    """What the UI-22 dialog shows before the operator presses OK."""

    press_id: str
    entry_number: int
    now: str
    contracts: int
    target_premium: Decimal
    wing_width: Decimal
    stop_loss_pct: int
    worst_case_estimate: Decimal
    effective_stop_pct_estimate: Decimal | None = None  # STP-02b (v1.67)

    def to_dict(self) -> dict[str, Any]:
        return {
            "press_id": self.press_id,
            "entry_number": self.entry_number,
            "now": self.now,
            "contracts": self.contracts,
            "target_premium": str(self.target_premium),
            "wing_width": str(self.wing_width),
            "stop_loss_pct": self.stop_loss_pct,
            "worst_case_estimate": str(self.worst_case_estimate),
            # v1.46: no strikes exist at press time, so the TRUE worst case cannot
            # be known here. RSK-04 re-prices from real strikes and may still veto.
            "worst_case_is_estimate": True,
            "estimate_formula": "(width - target premium) x 100 x contracts",
            # STP-02b (v1.67): alongside the worst-case disclosure, same dialog,
            # same ESTIMATE honesty stance -- None when there is nothing to
            # estimate from.
            "effective_stop_pct_estimate": (
                None if self.effective_stop_pct_estimate is None
                else str(self.effective_stop_pct_estimate)),
        }


class ManualEntry:
    def __init__(self, comp, selector, market_gates, *,
                 risk=None, day=None, blocks=None, spot_provider=None,
                 calendar_label=None) -> None:
        self._comp = comp
        self._selector = selector          # async (when, n, config) -> (Condor|None, skip|None)
        self._gates = market_gates         # async () -> GateSnapshot
        self._risk = risk                  # () -> RiskSnapshot | None (sync or async)
        self._day = day                    # () -> "YYYY-MM-DD"
        # ENT-09: "reconcile-block and clock-drift checks" apply to a manual fire.
        # They sit OUTSIDE the ENT-03 gate chain, so attempt() cannot run them.
        self._blocks = blocks              # () -> skip reason | None
        # ENT-09b v1.57: () -> Decimal|None current spot, for the refuse-and-
        # re-pick check. `None` (no provider wired) means spot is unknowable --
        # the check is skipped rather than refusing on a guess (the same
        # honesty stance as `_risk`/`_blocks` being optional above).
        self._spot = spot_provider
        # CAL-06 (v1.71): (day) -> NO-TRADE label | None, from the calendar tag
        # store (fail-open per CAL-07). None (default, e.g. paper/pre-v1.71
        # callers) means "no calendar wired" -- never refuses, same polarity
        # as an unwired provider reads everywhere else in this feature.
        self._calendar_label = calendar_label
        self._consumed: set[str] = set()   # press ids already acted on (idempotency)

    # --- UI-22 -------------------------------------------------------------------
    def preview(self, press_id: str, entry_number: int, row) -> FirePreview:
        """The dialog's contents. Showing the ESTIMATE, labelled (v1.46)."""
        return FirePreview(
            press_id=press_id, entry_number=entry_number,
            now=self._comp.clock.now().isoformat(),
            contracts=row.contracts, target_premium=row.target_premium,
            wing_width=row.wing_width, stop_loss_pct=row.stop_loss_pct,
            worst_case_estimate=worst_case_estimate(row),
            effective_stop_pct_estimate=effective_stop_pct_estimate(row))

    def can_fire(self) -> bool:
        """UI-22: ▶ is enabled only while all three trade-enabling states permit
        entries (ARMED ∧ Stop Trading OFF ∧ Confirm Live ON)."""
        return self._comp.state.entries_enabled()

    def today(self) -> str:
        """The day bucket a fire stamps onto its entry_id/events. ENT-11(3): the
        ad-hoc 101+ numbering lane must scan the SAME day string, so the API
        layer (which has no access to `_day`) reads it through here rather than
        guessing its own — a mismatch would let two ad-hoc fires collide."""
        return self._day() if self._day else trading_day_str(self._comp.clock.now())

    # --- ENT-11/UI-25 --------------------------------------------------------------
    async def simulate(self, row) -> dict[str, Any]:
        """UI-25: a READ-ONLY preview. Runs the identical selector as a fire would,
        against the row's OWN parameters, entry_number 0 (a probe number — it is
        never persisted anywhere, so it can never collide with a real entry).

        This appends NO event, places NO order, and consumes nothing — it
        must call ONLY the selector, never `execute.attempt`.
        On success it shows the strikes/mids/credit/worst-case the row would get
        IF fired now; the real fire re-selects from fresh data and may differ
        (v1.46 estimate-honesty precedent) — hence `estimate_note` below.
        """
        condor, skip = await self._selector(self._comp.clock.now(), 0, _selection(row))
        if condor is None:
            return {"result": "skipped", "reason": skip}

        from .execute_entry import ExecuteEntryAttempt

        return {
            "result": "ok",
            "put_short": str(condor.put_short), "put_long": str(condor.put_wing),
            "call_short": str(condor.call_short), "call_long": str(condor.call_wing),
            "put_mid": str(condor.put_short_mid), "call_mid": str(condor.call_short_mid),
            "net_credit": str(condor.mid_credit),
            "worst_case": str(ExecuteEntryAttempt.worst_case(condor)),
            "contracts": condor.contracts,
            "estimate_note": ("simulation — the real fire re-selects from fresh "
                              "data and may differ"),
        }

    # --- ENT-09 ------------------------------------------------------------------
    async def fire(self, *, press_id: str, entry_number: int, row,
                   confirmed: bool, put_floor: Decimal | None = None,
                   call_floor: Decimal | None = None,
                   blackout_ack: bool = False) -> dict[str, Any]:
        """Fire one entry now. `press_id` makes a double-click idempotent.

        `put_floor`/`call_floor` (ENT-09b v1.57, manual-entry-only, per-press —
        never persisted schedule state): minimum short-strike floors from the
        ▶ dialog's optional toggle. `None`/`None` is every non-floor press and
        every pre-v1.57 caller.

        `blackout_ack` (CAL-06, v1.71): the OK dialog's explicit acknowledgment
        checkbox, required ONLY when today's ET day carries a NO-TRADE tag.
        Never hard-blocks (ENT-09's fresh-intent rationale) — refused with a
        distinct, label-carrying reason without it; evented and report-tagged
        `blackout_overridden` with it. Default False is every pre-v1.71 caller
        and every press made on an untagged day (where it has no effect).
        """
        # 1. UI-22: no OK, no order — and nothing recorded. A dismissed or
        # timed-out dialog must leave the log exactly as it found it.
        if not confirmed:
            return {"result": "not_confirmed"}

        day = self.today()

        # 1b. CAL-06 (v1.71): a NO-TRADE tag on today's ET day never hard-blocks
        # a manual fire (ENT-09's whole rationale is FRESH operator intent) —
        # but it is never silent either. Refused, distinctly, without the
        # explicit acknowledgment checkbox; proceeds — evented, and the
        # eventual fill report-tagged `blackout_overridden` — with it. "The
        # operator overriding the operator's own rule is sovereignty, not a
        # breach ... but it is never silent" (CAL-06).
        #
        # Checked BEFORE the press is claimed (final-review finding 3,
        # 2026-07-15): a refused unacknowledged fire must NOT consume the
        # press_id — the operator's acknowledged retry arrives with the SAME
        # press_id (the dialog holds it), and a consumed press would come
        # back `duplicate_press` and never fire. Safe pre-claim: the refusal
        # is deterministic for identical inputs and places no order, so the
        # dedupe's double-submit protection is preserved.
        blackout_label = self._calendar_label(day) if self._calendar_label else None
        blackout_overridden = blackout_label is not None and blackout_ack
        if blackout_label is not None and not blackout_ack:
            reason = f"blackout_unacknowledged:{blackout_label}"
            self._comp.events.append(
                EntrySkipped(date=day, entry_number=entry_number, reason=reason,
                             put_floor=put_floor, call_floor=call_floor))
            return {"result": "skipped", "reason": reason, "blackout_label": blackout_label}

        # 2. Idempotent per press. Claimed BEFORE any await, so two concurrent
        # confirmations of the same press cannot both pass this line.
        if press_id in self._consumed:
            return {"result": "duplicate_press", "press_id": press_id}
        self._consumed.add(press_id)

        # 2b. CAL-06: the acknowledgment is evented AFTER the press claim, so
        # a double-confirm of one press can never journal it twice.
        if blackout_overridden:
            self._comp.events.append(ManualFireBlackoutAcknowledged(
                day=day, label=blackout_label, at=self._comp.clock.now().isoformat()))

        # 3. The ▶ enablement rule. Refused before an attempt runs, so no order
        # and no EntryWindowOpened — the card shows the skip reason.
        if not self.can_fire():
            self._comp.events.append(
                EntrySkipped(date=day, entry_number=entry_number, reason="blocked",
                             put_floor=put_floor, call_floor=call_floor))
            return {"result": "blocked", "reason": "blocked",
                    "state": self._comp.state.blocking_state()}

        # 4. ENT-09: the reconcile-block (REC-02 -> RSK-03) and the clock-drift
        # check (RSK-07). A manual press is fresh intent, not a stale clock.
        blocked = self._blocks() if self._blocks else None
        if blocked is not None:
            self._comp.events.append(
                EntrySkipped(date=day, entry_number=entry_number, reason=blocked,
                             put_floor=put_floor, call_floor=call_floor))
            return {"result": "skipped", "reason": blocked}

        # ENT-05 v1.81: RETIRED -- no entry-count cap check here. The day is
        # bounded only by RSK-04 (below, inside attempt()) and the RSK-08
        # order cap.

        # 5b. ENT-09b v1.57 refuse-and-re-pick: if spot has crossed a selected
        # floor since the dialog opened, the fire is REFUSED outright — never
        # silently reinterpreted against a floor that no longer makes sense.
        if put_floor is not None or call_floor is not None:
            spot = self._spot() if self._spot else None
            if spot is not None and floor_inside_spot(spot, put_floor=put_floor,
                                                       call_floor=call_floor):
                self._comp.events.append(
                    EntrySkipped(date=day, entry_number=entry_number, reason="floor_inside_spot",
                                 put_floor=put_floor, call_floor=call_floor))
                return {"result": "skipped", "reason": "floor_inside_spot"}

        # 6. Selection at fire time, from fresh chain data — as a scheduled entry.
        when = self._comp.clock.now()
        condor, skip = await self._selector(when, entry_number, _selection(row),
                                            put_floor=put_floor, call_floor=call_floor)
        if condor is None:
            self._comp.events.append(
                EntrySkipped(date=day, entry_number=entry_number,
                             reason=skip or "selection_unavailable",
                             put_floor=put_floor, call_floor=call_floor))
            return {"result": "skipped", "reason": skip or "selection_unavailable"}

        # 6. THE identical pipeline. `bypass_window` is the only difference; the
        # ENT-03 chain, RSK-08 and RSK-04 all run inside attempt().
        #
        # ENT-10(3)/STP-01: the attempt is ATOMIC. A cancelled request handler
        # (client disconnect) must never abandon it mid-ladder — that would
        # orphan a live resting order at the broker — nor mid-hand-off, which
        # would leave a filled condor with no stop. The whole attempt→protect
        # unit is ONE shielded task; ensure_future keeps a strong reference so
        # it is never GC'd while it finishes in the background.
        async def _attempt_and_protect():
            outcome = await self._comp.execute.attempt(
                day=day, scheduled=when, condor=condor, gates=await self._gates(),
                risk=await _maybe_await(self._risk),   # sync (paper) OR async (live)
                bypass_window=True, stop=_stop(row), initiator=MANUAL,
                put_floor=put_floor, call_floor=call_floor,   # ENT-09b v1.57 audit
                blackout_overridden=blackout_overridden)      # CAL-06 (v1.71) audit

            if outcome.status != "FILLED":
                return {"result": "skipped", "reason": outcome.reason}

            entry_id = f"{day}#{condor.entry_number}"
            # STP-02 (2026-07-09 fix): protect off the ACTUAL fill credit.
            await self._comp._on_filled(entry_id, condor, _stop(row),
                                        fill_credit=outcome.fill_credit)   # STP-01
            return {"result": "filled", "entry_id": entry_id,
                    "initiator": MANUAL, "fill_credit": str(outcome.fill_credit),
                    "blackout_overridden": blackout_overridden}

        # The done-callback is the ONLY guaranteed observer of this task's
        # outcome once the awaiting request is gone (client disconnect
        # cancels the handler; the shielded task runs on). Shared with the
        # scheduled path (attempt_crash.py, 2026-07-14 incident): RSK-06
        # critical alert + an `attempt_crashed:<Type>` skip journaled iff no
        # CondorFilled/EntrySkipped landed first — carrying this press's
        # ENT-09b floors for audit, like every other manual skip above.
        attempt_task = asyncio.ensure_future(_attempt_and_protect())
        attempt_task.add_done_callback(
            alert_and_journal_crashed_attempt(self._comp, day, condor.entry_number,
                                              put_floor=put_floor, call_floor=call_floor))
        return await asyncio.shield(attempt_task)


def _selection(row):
    from meic.composition.live_selection import SelectionConfig
    return None if row is None else SelectionConfig.for_entry(row)


def _stop(row):
    from meic.application.execute_entry import StopParams
    from meic.domain.stop_policy import StopBasis
    if row is None:
        return None
    return StopParams(basis=StopBasis(row.stop_basis), pct=Decimal(row.stop_loss_pct),
                      markup=row.stop_rebate_markup)
