"""Persistent-state inventory — REC-07.

The definitive list of durable state (doc 01 REC-07): written on every change,
restored EXACTLY on every boot (process restart, crash, Docker recovery,
machine reboot), never reset by time or restart. Safe defaults apply on
**fresh install only** — a store that has never been written.

This is a thin typed façade over a StateStore (durable KV). Because every
value lives in the durable store, "restore on boot" is automatic: construct a
new PersistentState over the same store and every item reads back exactly.

REC-07 warns that extending the inventory is a spec amendment — so the item
set here is closed and matches the doc's ten entries. Item 8 (the event log
and entry/position state) is the SqliteEventStore itself, referenced by the
composition root, not duplicated in this KV.
"""
from __future__ import annotations

import json
from typing import Any

from .ports import StateStore

# REC-07 fresh-install safe defaults (TC-ENT-07 'Fresh install defaults safe':
# DISARMED, Stop Trading off, Confirm Live OFF). trading_mode defaults to the
# safe side — paper.
_DEFAULTS: dict[str, Any] = {
    "armed": False,           # (1) ENT-01a
    "stop_trading": False,    # (2) RSK-01
    "confirm_live": False,    # (3) ENT-01b
    "trading_mode": "paper",  # (4) DAY-05
    "entry_schedule": [],     # (5) standing schedule + per-entry params
    "config_version": None,   # (6) active config version
    "tpf_floors": {},         # (7) armed TPF floors (TPF-08), per entry_id
    "own_ledger": {},         # (9) OWN ledger + SUSPENDED/quarantine states
    "paper_cash_ledger": None,  # (10) paper cash ledger + sim positions
}


class PersistentState:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    # --- generic typed access -------------------------------------------------
    def _get(self, key: str) -> Any:
        raw = self._store.get(key)
        if raw is None:
            return _DEFAULTS[key]  # fresh install only
        return json.loads(raw)

    def _set(self, key: str, value: Any) -> None:
        if key not in _DEFAULTS:
            raise KeyError(f"{key!r} is not in the REC-07 inventory (extending it is a spec amendment)")
        self._store.set(key, json.dumps(value))

    # --- the ten items --------------------------------------------------------
    @property
    def armed(self) -> bool:
        return self._get("armed")

    @armed.setter
    def armed(self, v: bool) -> None:
        self._set("armed", v)

    @property
    def stop_trading(self) -> bool:
        return self._get("stop_trading")

    @stop_trading.setter
    def stop_trading(self, v: bool) -> None:
        self._set("stop_trading", v)

    @property
    def confirm_live(self) -> bool:
        return self._get("confirm_live")

    @confirm_live.setter
    def confirm_live(self, v: bool) -> None:
        self._set("confirm_live", v)

    @property
    def trading_mode(self) -> str:
        return self._get("trading_mode")

    @trading_mode.setter
    def trading_mode(self, v: str) -> None:
        if v not in ("paper", "live"):
            raise ValueError(f"trading_mode must be paper|live, got {v!r}")
        self._set("trading_mode", v)

    @property
    def entry_schedule(self) -> list[dict[str, Any]]:
        return self._get("entry_schedule")

    @entry_schedule.setter
    def entry_schedule(self, v: list[dict[str, Any]]) -> None:
        self._set("entry_schedule", v)

    @property
    def config_version(self) -> Any:
        return self._get("config_version")

    @config_version.setter
    def config_version(self, v: Any) -> None:
        self._set("config_version", v)

    @property
    def tpf_floors(self) -> dict[str, Any]:
        return self._get("tpf_floors")

    @tpf_floors.setter
    def tpf_floors(self, v: dict[str, Any]) -> None:
        self._set("tpf_floors", v)

    @property
    def own_ledger(self) -> dict[str, Any]:
        return self._get("own_ledger")

    @own_ledger.setter
    def own_ledger(self, v: dict[str, Any]) -> None:
        self._set("own_ledger", v)

    @property
    def paper_cash_ledger(self) -> Any:
        return self._get("paper_cash_ledger")

    @paper_cash_ledger.setter
    def paper_cash_ledger(self, v: Any) -> None:
        self._set("paper_cash_ledger", v)

    # --- entry gate (ENT-01/01a/01b): the three durable enabling states -------
    def may_arm(self) -> bool:
        """ENT-01a: arming requires at least one composed entry — arming an empty
        schedule is rejected (never a silent no-op)."""
        return bool(self.entry_schedule)

    def entries_enabled(self) -> bool:
        """Entries fire iff ARMED ∧ Stop Trading OFF ∧ Confirm Live ON
        (ENT-01b). Names the blocking state for the dashboard via
        blocking_state()."""
        return self.armed and not self.stop_trading and self.confirm_live

    def blocking_state(self) -> str | None:
        if not self.armed:
            return "DISARMED"
        if self.stop_trading:
            return "STOP_TRADING"
        if not self.confirm_live:
            return "CONFIRM_LIVE_OFF"
        return None
