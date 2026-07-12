"""ExitMonitor — the ONE orchestrator over the TPF floor and TPT target
(TPF-03/09, TPT-01..07). Per-entry confirmation state for both monitors lives
here, keyed by entry_id, so a health-tick caller (server.py's
`_evaluate_exits_once`) has exactly one object to hold across ticks — the
same role `_Snapshots` plays for chain freshness.

STRUCTURAL GUARANTEE (TPF-03/TPT-04, "NEVER broker-resting"): this module
imports nothing from `order_intent`, `ports`, or any broker gateway, and
constructs no order of any kind. Its only output is a boolean "fire now" per
call; the caller is the one that invokes `CloseEntry` (CLS-02, one close
path). See `tests/application/test_exit_monitor_structural.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from meic.application.tpf_monitor import TPFMonitor
from meic.application.tpt_monitor import TPTMonitor


@dataclass
class ExitMonitor:
    tp_confirmation_evals: int = 2
    _floor: dict = field(default_factory=dict)
    _target: dict = field(default_factory=dict)

    def evaluate_floor(self, entry_id: str, *, profit_pct: Decimal | None,
                       level: int, stale: bool) -> bool:
        """`profit_pct`/`level` are both PERCENTAGES — `breached`'s `<=` is
        unit-agnostic, so no dollar conversion is needed here at all (TPT-06's
        dollar feedback is a display-only concern, computed separately)."""
        mon = self._floor.setdefault(
            entry_id, TPFMonitor(tp_confirmation_evals=self.tp_confirmation_evals))
        if stale or profit_pct is None:
            return mon.evaluate(profit=Decimal("0"), floor=Decimal("0"), stale=True)
        return mon.evaluate(profit=profit_pct, floor=Decimal(level), stale=False)

    def evaluate_target(self, entry_id: str, *, profit_pct: Decimal | None,
                        level: int, stale: bool) -> bool:
        mon = self._target.setdefault(
            entry_id, TPTMonitor(tp_confirmation_evals=self.tp_confirmation_evals))
        if stale or profit_pct is None:
            return mon.evaluate(profit=Decimal("0"), target=Decimal("0"), stale=True)
        return mon.evaluate(profit=profit_pct, target=Decimal(level), stale=False)

    def disarm_target(self, entry_id: str) -> None:
        """TPT-05: any stop fill on the entry disarms the target PERMANENTLY.
        Drops the counter so a later re-arm (a NEW target on the SAME entry_id
        would be operator error, not expected — but defensively) starts
        clean rather than inheriting a stale streak."""
        self._target.pop(entry_id, None)

    def forget(self, entry_id: str) -> None:
        """The entry reached a terminal status (closed/expired) — drop both
        counters so a reused entry_id (should that ever happen) never
        inherits a stale streak."""
        self._floor.pop(entry_id, None)
        self._target.pop(entry_id, None)
