"""ManualEntry — ENT-09 (v1.44) manual fire, and the UI-22 confirmation dialog.

The operator presses ▶ on a schedule row and fires that entry on demand, outside
any scheduled window. The ONLY rule this bypasses is the ENT-02 window — which
exists to guard against STALE SCHEDULED INTENT, and a manual press is fresh intent
by definition. Everything else applies unreduced, because the fire goes through
the identical `ExecuteEntryAttempt.attempt()` as a scheduled entry:

    full ENT-03 gate chain, reconcile-block, clock drift, ENT-07 sequencing,
    RSK-08 order cap, RSK-04 max exposure, and ENT-05 max_entries_per_day.

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

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from meic.domain.events import EntrySkipped
from meic.domain.projection import day_report

from .schedule_service import worst_case_estimate

MANUAL = "manual_entry"


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
        }


class ManualEntry:
    def __init__(self, comp, selector, market_gates, *, max_entries_per_day=None,
                 risk=None, day=None) -> None:
        self._comp = comp
        self._selector = selector          # async (when, n, config) -> (Condor|None, skip|None)
        self._gates = market_gates         # async () -> GateSnapshot
        self._max_entries = max_entries_per_day
        self._risk = risk                  # () -> RiskSnapshot | None
        self._day = day                    # () -> "YYYY-MM-DD"
        self._consumed: set[str] = set()   # press ids already acted on (idempotency)

    # --- UI-22 -------------------------------------------------------------------
    def preview(self, press_id: str, entry_number: int, row) -> FirePreview:
        """The dialog's contents. Showing the ESTIMATE, labelled (v1.46)."""
        return FirePreview(
            press_id=press_id, entry_number=entry_number,
            now=self._comp.clock.now().isoformat(),
            contracts=row.contracts, target_premium=row.target_premium,
            wing_width=row.wing_width, stop_loss_pct=row.stop_loss_pct,
            worst_case_estimate=worst_case_estimate(row))

    def can_fire(self) -> bool:
        """UI-22: ▶ is enabled only while all three trade-enabling states permit
        entries (ARMED ∧ Stop Trading OFF ∧ Confirm Live ON)."""
        return self._comp.state.entries_enabled()

    # --- ENT-09 ------------------------------------------------------------------
    async def fire(self, *, press_id: str, entry_number: int, row,
                   confirmed: bool) -> dict[str, Any]:
        """Fire one entry now. `press_id` makes a double-click idempotent."""
        # 1. UI-22: no OK, no order — and nothing recorded. A dismissed or
        # timed-out dialog must leave the log exactly as it found it.
        if not confirmed:
            return {"result": "not_confirmed"}

        # 2. Idempotent per press. Claimed BEFORE any await, so two concurrent
        # confirmations of the same press cannot both pass this line.
        if press_id in self._consumed:
            return {"result": "duplicate_press", "press_id": press_id}
        self._consumed.add(press_id)

        day = self._day() if self._day else self._comp.clock.now().date().isoformat()

        # 3. The ▶ enablement rule. Refused before an attempt runs, so no order
        # and no EntryWindowOpened — the card shows the skip reason.
        if not self.can_fire():
            self._comp.events.append(
                EntrySkipped(date=day, entry_number=entry_number, reason="blocked"))
            return {"result": "blocked", "reason": "blocked",
                    "state": self._comp.state.blocking_state()}

        # 4. ENT-05: a manual entry COUNTS toward max_entries_per_day.
        if self._max_entries is not None and self._filled_today() >= self._max_entries:
            self._comp.events.append(
                EntrySkipped(date=day, entry_number=entry_number, reason="max_entries"))
            return {"result": "skipped", "reason": "max_entries"}

        # 5. Selection at fire time, from fresh chain data — as a scheduled entry.
        when = self._comp.clock.now()
        condor, skip = await self._selector(when, entry_number, _selection(row))
        if condor is None:
            self._comp.events.append(
                EntrySkipped(date=day, entry_number=entry_number,
                             reason=skip or "selection_unavailable"))
            return {"result": "skipped", "reason": skip or "selection_unavailable"}

        # 6. THE identical pipeline. `bypass_window` is the only difference; the
        # ENT-03 chain, RSK-08 and RSK-04 all run inside attempt().
        outcome = await self._comp.execute.attempt(
            day=day, scheduled=when, condor=condor, gates=await self._gates(),
            risk=self._risk() if self._risk else None,
            bypass_window=True, stop=_stop(row), initiator=MANUAL)

        if outcome.status != "FILLED":
            return {"result": "skipped", "reason": outcome.reason}

        entry_id = f"{day}#{condor.entry_number}"
        await self._comp._on_filled(entry_id, condor, _stop(row))   # STP-01
        return {"result": "filled", "entry_id": entry_id,
                "initiator": MANUAL, "fill_credit": str(outcome.fill_credit)}

    def _filled_today(self) -> int:
        """ENT-05 counts FILLS, not attempts — the same rule day_report uses."""
        return day_report(self._comp.events).entries_filled


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
