"""RPT-16/doc-10-Principle-1 structural guarantee for the BACKFILL service
(mirrors test_report_reconciler_structural.py, applied to
application/backfill.py): the ONLY module RPT-16's one-time import is
allowed to reach a broker through is itself, and it must import nothing
from `meic.adapters` or `meic.composition`, and reference no
submit/replace/cancel capability anywhere in its source text.
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKFILL_PATH = (Path(__file__).resolve().parents[2] / "backend" / "src" / "meic"
                 / "application" / "backfill.py")
FORBIDDEN_PREFIXES = ("meic.adapters", "meic.composition")
FORBIDDEN_CALLS = (".submit(", ".replace(", ".cancel(")


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_backfill_has_no_broker_gateway_import():
    tree = ast.parse(BACKFILL_PATH.read_text(encoding="utf-8"))
    hits = {m for m in _imported_modules(tree)
            if any(m == p or m.startswith(p + ".") for p in FORBIDDEN_PREFIXES)}
    assert not hits, f"backfill.py imports adapters/composition: {hits}"


def test_backfill_source_has_no_order_action_capability():
    text = BACKFILL_PATH.read_text(encoding="utf-8")
    hits = [c for c in FORBIDDEN_CALLS if c in text]
    assert not hits, f"backfill.py references order-action calls: {hits}"


def test_backfill_facade_protocol_declares_only_read_methods():
    """The narrow `BackfillBrokerFacade` Protocol this module defines carries
    ONLY the two read fetches RPT-16 needs (Trade fills + Receive-Deliver
    settlements, operator ruling 2026-07-10) -- never an order-action method."""
    from meic.application.backfill import BackfillBrokerFacade

    methods = {name for name in vars(BackfillBrokerFacade) if not name.startswith("_")}
    assert methods == {"day_fills", "day_settlements"}
