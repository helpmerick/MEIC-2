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
from meic.application.persistent_state import PersistentState

FLATTEN_CONFIRMATION = "FLATTEN"


@dataclass(frozen=True)
class CloseResult:
    result: str      # "closed" | "cancelled" | "already_done"
    initiator: str   # "manual" | "cancel_entry"


@dataclass
class ManualClose:
    close_entry: CloseEntry
    broker: object
    state: PersistentState
    _done: set = field(default_factory=set)

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
