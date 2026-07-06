"""ENT-03 pre-entry gate chain (pure decision).

Before each entry attempt the bot verifies, IN ORDER (ENT-03): ARMED,
Confirm Live ON, Stop Trading off, no Flatten All executing, market open and
not halted, market data fresh, broker session valid, buying power sufficient.
The first failure names the skip reason and the entry is skipped — never
executed partway.

Pure: evaluates a snapshot and returns the reason (or None). The application
service takes the snapshot from PersistentState + market/broker adapters and
acts on the verdict.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateSnapshot:
    armed: bool
    confirm_live: bool
    stop_trading: bool
    flatten_in_progress: bool
    market_open: bool
    market_halted: bool
    data_fresh: bool
    session_valid: bool
    buying_power_ok: bool


def evaluate_gates(s: GateSnapshot) -> str | None:
    """Return the first failing gate's skip reason, or None if all pass.
    Order and reasons are ENT-03 / TC-ENT-02 canonical."""
    if not s.armed:
        return "disarmed"                 # ENT-01a
    if not s.confirm_live:
        return "confirm_live_off"         # ENT-01b
    if s.stop_trading:
        return "stop_trading"             # RSK-01
    if s.flatten_in_progress:
        return "flatten_in_progress"      # RSK-01a
    if not s.market_open or s.market_halted:
        return "market_halted"            # DAT-04
    if not s.data_fresh:
        return "data_unavailable"         # DAT-02
    if not s.session_valid:
        return "invalid_session"          # broker session
    if not s.buying_power_ok:
        return "insufficient_bp"          # worst-case margin
    return None
