"""Step definition for TC-NFR-07's second scenario -- the STP-03 (v1.67)
stop_limit tombstone absence test.

Only THIS scenario is bound here (`@scenario`, not `scenarios()`): TC-NFR-07's
first scenario (the NFR-07 wiring-audit registry / DecayWatcher regression) is
a separate, much larger ratified item that is not part of this change and is
deliberately left unbound rather than half-implemented.

Background (07-13 week-review): `application/order_intent.py` hardcoded
`order_type="stop_market"` everywhere a stop was built -- no production code
path ever constructed a `stop_limit` order. Meanwhile `application/
stop_escalation.py` (the EC-STP-08 unfilled-escalation watchdog stop_limit
would have needed) was built, unit-tested, and had exactly two references
repo-wide: itself and its own test file. STP-03 ratified "retire, don't
build": the module is deleted, `stop_limit` is removed from every order-type
vocabulary so constructing one raises at the OrderIntent boundary, and the
config loader rejects `stop_order_type` outright. This test pins the absence
so a future re-add fails loudly, in three independent ways: the vocabulary,
runtime construction, and a source-level scan for anyone routing around the
frozensets entirely.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from pytest_bdd import scenario, then

from meic.application.order_intent import (
    ORDER_TYPES,
    PRICED_TYPES,
    STOP_TYPES,
    IntentError,
    OrderIntent,
)
from meic.config.validation import (
    TOMBSTONE_KEYS_V167,
    ConfigRejected,
    validate_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MEIC_SRC = REPO_ROOT / "backend" / "src" / "meic"

_CONSTRUCTION_PATTERN = re.compile(
    r'order_type\s*=\s*["\']stop_limit["\']'   # OrderIntent(order_type="stop_limit", ...)
    r'|OrderType\.STOP_LIMIT'                   # the SDK enum member, referenced directly
    r'|["\']stop_limit["\']\s*:\s*\w'           # a type_map-style {"stop_limit": ...} entry
)


@scenario("../features/TC-NFR-07.feature", "stop_limit has no construction path (STP-03 tombstone)")
def test_stop_limit_tombstone_absence():
    pass


@then("no code constructs a stop_limit order and the config loader rejects stop_order_type")
def _():
    # Delegates to the standalone assertions below so the same checks run both
    # as this bound Gherkin step AND as independently discoverable pytest tests.
    test_stop_limit_absent_from_every_order_type_vocabulary()
    test_constructing_a_stop_limit_order_intent_raises()
    test_config_loader_rejects_stop_order_type()
    test_no_production_source_constructs_a_stop_limit_order()


# --- vocabulary: stop_limit is not a member of any order-type set ------------

def test_stop_limit_absent_from_every_order_type_vocabulary():
    assert "stop_limit" not in ORDER_TYPES
    assert "stop_limit" not in STOP_TYPES
    assert "stop_limit" not in PRICED_TYPES


# --- runtime: constructing one raises, it does not silently succeed ---------

def test_constructing_a_stop_limit_order_intent_raises():
    with pytest.raises(IntentError):
        OrderIntent(order_type="stop_limit", tif="Day", legs=(), contracts=1)


# --- config: stop_order_type and the dead escalation window are rejected ----

def test_config_loader_rejects_stop_order_type():
    with pytest.raises(ConfigRejected) as exc:
        validate_config({"stop_order_type": "stop_limit"})
    assert exc.value.key == "stop_order_type"


def test_config_loader_rejects_stop_limit_escalation_seconds():
    with pytest.raises(ConfigRejected) as exc:
        validate_config({"stop_limit_escalation_seconds": 10})
    assert exc.value.key == "stop_limit_escalation_seconds"


def test_the_v167_tombstone_set_is_exactly_these_two_keys():
    assert TOMBSTONE_KEYS_V167 == frozenset({"stop_order_type", "stop_limit_escalation_seconds"})


# --- the dead EC-STP-08 module is actually deleted, not merely unwired ------

def test_stop_escalation_module_no_longer_exists():
    assert importlib.util.find_spec("meic.application.stop_escalation") is None


# --- source-level scan: no production file builds a stop_limit order, even
# by routing around the vocabulary frozensets above (belt and suspenders --
# the frozenset checks alone would miss a parallel construction path). Full-
# line comments (this file's own explanatory prose included) are excluded so
# documenting the absence does not trip the very test that proves it.

def _is_commented(line: str, match_start: int) -> bool:
    hash_pos = line.find("#")
    return hash_pos != -1 and hash_pos < match_start


def test_no_production_source_constructs_a_stop_limit_order():
    violations: list[str] = []
    for path in MEIC_SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in _CONSTRUCTION_PATTERN.finditer(line):
                if not _is_commented(line, m.start()):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert violations == [], (
        "a production source constructs a stop_limit order -- STP-03 tombstone "
        f"violated: {violations}"
    )
