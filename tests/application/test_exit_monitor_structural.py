"""TPF-03/TPT-04 structural guarantee ("NEVER broker-resting"), mirroring
`tests/application/test_report_reconciler_structural.py`'s AST scan applied
to the profit-monitor modules: none of them may import a broker/order-intent
capability or construct/submit an order of any kind. Their only allowed
output is a boolean "fire now" that the CALLER routes through CloseEntry
(CLS-02) — see `tests/adapters/test_exit_evaluator.py` for that wiring.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "backend" / "src" / "meic"
MONITOR_PATHS = [
    SRC / "domain" / "tpf.py",
    SRC / "domain" / "tpt.py",
    SRC / "application" / "tpf_monitor.py",
    SRC / "application" / "tpt_monitor.py",
    SRC / "application" / "exit_monitor.py",
]
FORBIDDEN_PREFIXES = ("meic.adapters", "meic.composition")
FORBIDDEN_CALLS = (".submit(", ".replace(", ".cancel(")
FORBIDDEN_IMPORTS = ("order_intent", "ports", "cancel_taxonomy")


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_no_monitor_module_imports_adapters_or_composition():
    for path in MONITOR_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = {m for m in _imported_modules(tree)
                if any(m == p or m.startswith(p + ".") for p in FORBIDDEN_PREFIXES)}
        assert not hits, f"{path.name} imports adapters/composition: {hits}"


def test_no_monitor_module_references_order_action_calls():
    for path in MONITOR_PATHS:
        text = path.read_text(encoding="utf-8")
        hits = [c for c in FORBIDDEN_CALLS if c in text]
        assert not hits, f"{path.name} references order-action calls: {hits}"


def test_no_monitor_module_imports_order_construction_helpers():
    for path in MONITOR_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = _imported_modules(tree)
        hits = {m for m in modules if any(needle in m for needle in FORBIDDEN_IMPORTS)}
        assert not hits, f"{path.name} imports order-construction helpers: {hits}"


def test_exit_monitor_public_surface_returns_only_booleans_or_none():
    """ExitMonitor's evaluate_* methods are the ONLY thing a caller acts on —
    they return a plain bool (fire now / don't), never an order/intent object
    a caller could mistake for something to submit directly."""
    from decimal import Decimal

    from meic.application.exit_monitor import ExitMonitor

    mon = ExitMonitor(tp_confirmation_evals=1)
    result = mon.evaluate_floor("e1", profit_pct=Decimal("0"), level=50, stale=False)
    assert isinstance(result, bool)
    result = mon.evaluate_target("e1", profit_pct=Decimal("100"), level=50, stale=False)
    assert isinstance(result, bool)
