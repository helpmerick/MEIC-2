"""RPT-15/doc-10-Principle-1 structural guarantee for the RECONCILER (mirrors
TC-RPT-06's AST scan of `meic.reporting`, tests/bdd/test_tc_rpt_06.py, applied
to `application/report_reconciler.py`): the ONLY module the RPT-15
reconciler is allowed to reach a broker through is itself, and it must import
nothing from `meic.adapters` or `meic.composition`, and reference no
submit/replace/cancel capability anywhere in its source text.
"""
from __future__ import annotations

import ast
from pathlib import Path

RECONCILER_PATH = (Path(__file__).resolve().parents[2] / "backend" / "src" / "meic"
                   / "application" / "report_reconciler.py")
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


def test_reconciler_has_no_broker_gateway_import():
    tree = ast.parse(RECONCILER_PATH.read_text(encoding="utf-8"))
    hits = {m for m in _imported_modules(tree)
            if any(m == p or m.startswith(p + ".") for p in FORBIDDEN_PREFIXES)}
    assert not hits, f"report_reconciler.py imports adapters/composition: {hits}"


def test_reconciler_source_has_no_order_action_capability():
    text = RECONCILER_PATH.read_text(encoding="utf-8")
    hits = [c for c in FORBIDDEN_CALLS if c in text]
    assert not hits, f"report_reconciler.py references order-action calls: {hits}"


def test_reconciler_facade_protocol_declares_only_read_methods():
    """The narrow `ReadOnlyBrokerFacade` Protocol this module defines carries
    ONLY the four read fetches RPT-15 needs (OWN-01/OWN-03 fix added
    `day_settlements`, the same read `capture_settlements`/`backfill_day`
    already use, so the reconciler can scope settlement rows to the bot's
    own symbols too) -- never an order-action method, so nothing that
    duck-types against it can be mistaken for a full BrokerGateway."""
    from meic.application.report_reconciler import ReadOnlyBrokerFacade

    methods = {name for name in vars(ReadOnlyBrokerFacade) if not name.startswith("_")}
    assert methods == {"positions", "day_fills", "day_settlements", "cash_and_fees"}
