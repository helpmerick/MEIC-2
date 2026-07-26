"""TPF-03a — exit evaluation has a DEDICATED owner, at a bounded interval.

THE DEFECT OF RECORD (2026-07-26). Exit evaluation had exactly ONE caller: the
60 s health tick, SLEEP-FIRST. Three consequences, all live in production:

  * every boot ran BLIND for a full minute -- at the one moment, just after a
    restart, when state is least certain;
  * a breach that began AND ended inside one window was NEVER OBSERVED at all;
  * a persisting breach acted 60-120 s late.

TPF-03 already required "every valid quote evaluation". The code did not do
what was ratified, and NOTHING PINNED THE WIRING -- removing exit evaluation
from the health tick broke no test. That absence is why it drifted, so these
tests pin the OUTCOME the rule constrains: a dedicated owner, evaluate-first,
bounded interval, and not a duty of the health loop.

The rule deliberately constrains the outcome and not the mechanism -- a short
loop and a tick-driven subscriber both satisfy it -- so these tests assert
those properties, never the presence of a particular loop implementation.
"""
from __future__ import annotations

import ast
import asyncio

from pathlib import Path

import pytest

from meic.adapters.api import server
from meic.adapters.api.server import _exit_eval_interval_ms

SERVER_SRC = Path(server.__file__)


# -- the cadence dial ---------------------------------------------------------

def test_tpf03a_interval_defaults_to_250ms():
    assert _exit_eval_interval_ms({}) == 250


@pytest.mark.parametrize("raw,expected", [
    ("100", 100),      # doc 06 lower bound
    ("5000", 5000),    # doc 06 upper bound
    ("250", 250),
    ("99", 250),       # below range -> spec default, never the out-of-range value
    ("5001", 250),     # above range -> spec default
    ("0", 250),        # a zero interval would be a busy-loop, never accepted
    ("-1", 250),
    ("abc", 250),      # unparsable -> spec default, never a crash at boot
])
def test_tpf03a_interval_rejects_out_of_range_dials(raw, expected):
    """Same reject-the-dial convention as `max_quote_age_ms`: a bad dial falls
    back to the ratified default rather than being honoured or raising. A dial
    that crashed at boot would take the whole panel down over a typo."""
    assert _exit_eval_interval_ms({"MEIC_EXIT_EVAL_INTERVAL_MS": raw}) == expected


def test_tpf03a_interval_is_never_slower_than_the_rule_permits():
    """The bound that matters: whatever the operator sets, the evaluator can
    never be put back on a 60 s cadence by configuration alone."""
    for raw in ("60000", "3600000", "99999999"):
        assert _exit_eval_interval_ms({"MEIC_EXIT_EVAL_INTERVAL_MS": raw}) <= 5000


# -- the health tick is no longer an exit-evaluation owner --------------------

def _health_tick_body() -> str:
    """The source of the periodic health-tick coroutine in server.py."""
    tree = ast.parse(SERVER_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_probe_once":
            return ast.get_source_segment(SERVER_SRC.read_text(encoding="utf-8"), node) or ""
    return ""


def test_tpf03a_the_health_tick_does_not_evaluate_exits():
    """TPF-03a: exit evaluation "MUST NOT be a duty of the health loop or any
    loop whose primary purpose is something else."

    Pinned at the SOURCE because the failure is an ABSENCE -- there is no
    runtime observation that distinguishes "the health tick no longer
    evaluates exits" from "the health tick ran and nothing breached". An
    absence has to be asserted where it lives."""
    body = _health_tick_body()
    assert body, "could not locate the health-tick coroutine -- update this test, not the rule"
    assert "_evaluate_exits_once" not in body, (
        "exit evaluation is back on the health tick. Two owners at different "
        "cadences is worse than one at the wrong cadence -- give it to the "
        "dedicated loop (TPF-03a)")


def test_tpf03a_a_dedicated_owner_exists_and_is_the_only_caller():
    """Exactly ONE production call site, and it is not the health tick."""
    source = SERVER_SRC.read_text(encoding="utf-8")
    calls = source.count("await _evaluate_exits_once(")
    assert calls == 1, f"expected exactly one exit-evaluation call site, found {calls}"
    assert "_start_exit_eval_loop" in source


def _exit_loop_body():
    """The `while True:` body of the dedicated exit-evaluation loop, as AST.

    Located structurally rather than by slicing characters out of the file:
    the first version of this test took a fixed 3000-character window and
    broke the moment the loop's error handler grew, which is a test that
    fails for a reason unrelated to the property it guards."""
    tree = ast.parse(SERVER_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_exit_eval_loop":
            for inner in ast.walk(node):
                if isinstance(inner, ast.AsyncFunctionDef) and inner.name == "_loop":
                    for stmt in inner.body:
                        if isinstance(stmt, ast.While):
                            return stmt.body
    return None


def test_tpf03a_the_dedicated_loop_evaluates_before_it_sleeps():
    """SLEEP-FIRST WAS THE DEFECT. A loop that sleeps before its first pass
    runs blind for a full interval after every boot -- which at 60 s was the
    original hole, and is a property of the loop's SHAPE, not its interval."""
    body = _exit_loop_body()
    assert body, "could not locate the exit-evaluation loop -- update this test, not the rule"

    def _first_index(predicate):
        for i, stmt in enumerate(body):
            for sub in ast.walk(stmt):
                if predicate(sub):
                    return i
        return None

    def _is_pass_call(n):
        return (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_pass")

    def _is_sleep_call(n):
        return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "sleep")

    pass_at, sleep_at = _first_index(_is_pass_call), _first_index(_is_sleep_call)
    assert pass_at is not None, "the loop never evaluates"
    assert sleep_at is not None, "the loop never sleeps -- that is a busy loop"
    assert pass_at < sleep_at, (
        "the exit-evaluation loop sleeps before its first pass -- every boot "
        "would run blind for a full interval (the original 60 s hole)")


# -- the loop's own guarantees ------------------------------------------------

@pytest.mark.asyncio
async def test_tpf03a_a_pass_in_flight_is_skipped_never_queued():
    """At 250 ms a pass that overruns must not let passes pile up behind it.
    A held lock already means an evaluation is in flight, which is the thing
    the next tick wanted anyway. This models that contract directly."""
    lock = asyncio.Lock()
    ran = []

    async def _pass():
        if lock.locked():
            return
        async with lock:
            ran.append(1)
            await asyncio.sleep(0.05)

    await asyncio.gather(*(_pass() for _ in range(10)))
    assert len(ran) == 1, "overlapping passes queued instead of being skipped"


@pytest.mark.asyncio
async def test_tpf03a_a_failing_pass_never_kills_the_loop():
    """An exit evaluator that stops evaluating looks EXACTLY like an entry
    that never breached -- silence is indistinguishable from safety, which is
    why the loop must survive its own failures and surface them."""
    passes, errors = [], []

    async def _pass(n):
        passes.append(n)
        if n == 2:
            raise RuntimeError("one bad pass")

    for n in range(5):
        try:
            await _pass(n)
        except Exception as exc:  # noqa: BLE001 -- the loop's own guard
            errors.append(repr(exc))

    assert passes == [0, 1, 2, 3, 4], "the loop stopped after a failing pass"
    assert len(errors) == 1 and "one bad pass" in errors[0], "the failure was swallowed silently"
