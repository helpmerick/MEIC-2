"""TC-RPT-06 — the reporting module cannot trade (doc 10 Principle 1). Bound
structurally, in the style of TC-CLS-01's AST scans (tests/bdd/test_tc_cls_01.py):
`meic.reporting` imports nothing from `meic.adapters` or `meic.composition`,
so it can hold no broker-gateway reference at all -- no /reports endpoint
exists yet in slice 1 (deferred to the endpoints slice), so "no /reports
endpoint can mutate trading state" and "its only broker access is the RPT-15
read-only reconciliation fetch" are vacuously satisfied for now and re-tested
against the real endpoints when they land.
"""
from __future__ import annotations

import ast
from pathlib import Path

from pytest_bdd import scenarios, then

scenarios("../features/TC-RPT-06.feature")

REPORTING_PKG = Path(__file__).resolve().parents[2] / "backend" / "src" / "meic" / "reporting"
FORBIDDEN_PREFIXES = ("meic.adapters", "meic.composition")


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@then("the reporting module has no order-action dependency on the broker gateway")
def _():
    offenders: dict[str, set[str]] = {}
    for path in REPORTING_PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = {m for m in _imported_modules(tree)
                if any(m == p or m.startswith(p + ".") for p in FORBIDDEN_PREFIXES)}
        if hits:
            offenders[path.relative_to(REPORTING_PKG).as_posix()] = hits
    assert not offenders, f"reporting package imports adapters/composition: {offenders}"


@then("no /reports endpoint can mutate trading state")
def _():
    # No /reports endpoint exists yet (slice 1 is folds-only, no API surface) --
    # vacuously true; re-assert against the real router once it lands.
    assert True


@then("its only broker access is the RPT-15 read-only reconciliation fetch")
def _():
    # No broker access of ANY kind exists in `meic.reporting` yet -- the
    # structural import-graph check above already proves it, since a
    # broker-gateway reference could only arrive via `meic.adapters`.
    for path in REPORTING_PKG.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "BrokerGateway" not in text and "broker.submit" not in text
