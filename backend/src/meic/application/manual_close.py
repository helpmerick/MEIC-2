"""Manual close / cancel command — UC-14 / UI-16 / CLS-02.

The operator's Close action fires INSTANTLY with no confirmation dialog (Bug
#16): it routes through the one canonical CloseEntry (initiator `manual`),
clears any armed TPF floor for that entry, and is idempotent — a rapid
double-click produces exactly one close (ORD-04/CLS-03). A WORKING (pre-fill)
entry is CANCELLED instead (CLS-03), also instant, with no close orders placed
for its unfilled legs. Flatten-all is the ONE control that still requires a
typed `FLATTEN` confirmation (TC-FLT-01). Failures are returned to the caller
so the UI can render a toast — never a blocking dialog.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.execute_entry import _fill_matches  # reused normalizer, never a new one
from meic.application.persistent_state import PersistentState
from meic.domain.events import ReconciliationMismatch

FLATTEN_CONFIRMATION = "FLATTEN"


class _NoOpAlerts:
    def alert(self, level: str, message: str, **context) -> None:  # pragma: no cover - trivial
        pass


@dataclass(frozen=True)
class CloseResult:
    result: str      # "closed" | "cancelled" | "already_done" | "race_detected"
    initiator: str   # "manual" | "cancel_entry"


@dataclass
class ManualClose:
    close_entry: CloseEntry
    broker: object
    state: PersistentState
    _done: set = field(default_factory=set)
    # REPRICE-RACE SWEEP (2026-07-11): `ManualClose` is not wired into the
    # live/paper composition today (grep confirms no `ManualClose(` outside
    # this module and its own unit test — the real Close button routes through
    # `panel_commands.PanelCommands.close_as`, straight to `CloseEntry`, and
    # nothing wires this class's `cancel_working` CLS-03 path at all). `alerts`
    # and `events` are added here preventatively — a no-op alerts sink and an
    # empty log by default — so wiring this class in later cannot resurrect
    # the class of race this sweep exists to close.
    alerts: object = field(default_factory=_NoOpAlerts)
    events: list = field(default_factory=list)

    def requires_close_confirmation(self) -> bool:
        """UI-16 / Bug #16: Close never asks — it fires instantly, no dialog."""
        return False

    async def close(self, entry_id: str, *, live_legs: list[LiveLeg],
                    resting_stop_ids: dict[str, str], close_price) -> CloseResult:
        """Close a filled entry via CLS (initiator `manual`); clear its TPF
        floor. Idempotent: a second call is a no-op (no duplicate orders)."""
        if entry_id in self._done:
            return CloseResult("already_done", "manual")
        self._done.add(entry_id)
        await self.close_entry.close(
            entry_id, "manual", resting_stop_ids=resting_stop_ids,
            live_legs=live_legs, close_price=close_price)
        self._clear_tpf_floor(entry_id)
        return CloseResult("closed", "manual")

    async def cancel_working(self, entry_id: str, order_id: str) -> CloseResult:
        """CLS-03: a WORKING entry is cancelled (instant) — no close orders are
        placed for its unfilled legs. Idempotent like close()."""
        if entry_id in self._done:
            return CloseResult("already_done", "cancel_entry")
        self._done.add(entry_id)
        await self.broker.cancel(order_id)
        # REPRICE-RACE SWEEP (2026-07-11): the entry can fill in the window
        # between the operator's click and this cancel — neither adapter's
        # cancel() reliably reports "it was already filled" (SimulatedBroker:
        # {"result": "terminal", ...}; TastytradeAdapter: {"result": "error",
        # ...} for any cancel failure). Trusting it blindly would report
        # "cancelled" for a condor that is, in fact, live and unprotected — no
        # CondorFilled, no stop, no alert. This module has no strike/leg
        # information to reconstruct the entry (ORD-09), so it never guesses;
        # it surfaces the race loudly, same as reconcile.py's own boot-cancel
        # guard, and returns a distinct result so a caller never treats it as
        # a clean cancel.
        if any(_fill_matches(f, order_id) for f in await self.broker.fills_since(None)):
            detail = (f"CLS-03 cancel of working entry {entry_id} (order {order_id}) "
                     "raced a fill — position may be unprotected; operator must "
                     "reconcile manually")
            self.events.append(ReconciliationMismatch(detail=detail))
            self.alerts.alert("critical", detail, entry_id=entry_id, order_id=order_id)
            self._clear_tpf_floor(entry_id)
            return CloseResult("race_detected", "cancel_entry")
        self._clear_tpf_floor(entry_id)
        return CloseResult("cancelled", "cancel_entry")

    def _clear_tpf_floor(self, entry_id: str) -> None:
        floors = dict(self.state.tpf_floors)
        if floors.pop(entry_id, None) is not None:
            self.state.tpf_floors = floors

    @staticmethod
    def may_flatten(confirmation: str) -> bool:
        """TC-FLT-01: flatten-all is the one action gated on a typed FLATTEN."""
        return confirmation == FLATTEN_CONFIRMATION
