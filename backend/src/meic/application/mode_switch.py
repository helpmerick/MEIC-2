"""Trading-mode promotion — UC-10 / DAY-05 (pure decision + next-day apply).

Mode is fixed for the whole trading day. A switch requires a FLAT book (no open
positions, no working orders) and takes effect NEXT DAY start — never intraday.
Promotion to live additionally requires the operator to type the word LIVE
(one more deliberate layer before real money). The staged target lives in the
durable event log (a ModeSwitchStaged event, REC-07 item 8) so it survives a
restart and is read at the next day's boot — no new persistent-state item.
"""
from __future__ import annotations

from dataclasses import dataclass

from meic.domain.events import Event, ModeSwitchStaged

CONFIRM_TOKEN = "LIVE"
MODES = ("paper", "live")


@dataclass(frozen=True)
class ModeSwitchResult:
    staged: bool
    target: str
    effective: str = "next_day"
    reason: str | None = None


def request_mode_switch(
    *,
    target: str,
    current: str,
    open_positions: int,
    working_orders: int,
    confirmation: str = "",
) -> ModeSwitchResult:
    """Validate a mode-switch request. Staged (effective next day) only when the
    book is flat and — for live — the LIVE confirmation was typed."""
    if target not in MODES:
        return ModeSwitchResult(False, target, reason="unknown_mode")
    if target == current:
        return ModeSwitchResult(False, target, reason="already_in_mode")
    if open_positions != 0 or working_orders != 0:
        return ModeSwitchResult(False, target, reason="book_not_flat")  # DAY-05
    if target == "live" and confirmation != CONFIRM_TOKEN:
        return ModeSwitchResult(False, target, reason="confirmation_required")  # UC-10
    return ModeSwitchResult(True, target)


def pending_mode(events: list[Event]) -> str | None:
    """The last staged target in the log, or None. Read at day boot."""
    staged = [e for e in events if isinstance(e, ModeSwitchStaged)]
    return staged[-1].target if staged else None


def apply_pending_mode(state, events: list[Event]) -> str:
    """DAY-05 next-day application: at day start, adopt the last staged mode (if
    any) into trading_mode. Idempotent — re-applying the current mode is a no-op."""
    target = pending_mode(events)
    if target and target != state.trading_mode:
        state.trading_mode = target
    return state.trading_mode
