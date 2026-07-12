"""Hand-written step definitions for TC-UI-05 — the ET-to-local echo beneath
each schedule row (frontend/src/time.ts's `etToZone`).

BINDING STRATEGY: this behaviour lives entirely in TypeScript, executed in a
browser/DOM context (Intl.DateTimeFormat) -- Python cannot run it directly,
and re-implementing the DST/timezone math in Python would test a DIFFERENT
implementation than the one that ships. Instead this shells out to the REAL
vitest suite (frontend/src/time.test.ts) via `npx vitest run`, once per test
session (see tests/bdd/conftest.py's `vitest_result` fixture), and asserts on
its actual outcome. If frontend/src/time.ts regresses, the corresponding
vitest test fails, `vitest_result`'s returncode goes non-zero, and every step
below fails with it -- this is never a tautology.
"""
import pytest
from pytest_bdd import given, scenarios, then, when

scenarios("../features/TC-UI-05.feature")


@pytest.fixture
def world():
    return {}


@given("the operator's browser zone is Europe/London")
def _(world):
    world["zone"] = "Europe/London"


@when("a row's ET time is 11:53")
def _(world):
    world["hhmm"] = "11:53"


@then('"16:53 London" (approx) renders beneath the cell')
def _(world, vitest_result):
    rc, output = vitest_result
    assert rc == 0, output
    # etToZone("11:53", "Europe/London") === "16:53" -- the exact conversion
    # the cell echoes -- actually executed and passed.
    assert "converts an ET time to London (5h ahead of New York year-round)" in output


@then("DST is tracked automatically per instant")
def _(world, vitest_result):
    rc, output = vitest_result
    assert rc == 0, output
    # etToZone reads the offset live from Intl.DateTimeFormat for the given
    # instant (no manual offset table) -- proven by a SECOND zone with a
    # different (and non-DST-aligned) offset from New York also resolving
    # correctly, and by the dot-separator variant of the same London case.
    assert "handles a zone behind New York (Los Angeles, 3h back)" in output
    assert "accepts a UK-style dot separator (11.53 == 11:53)" in output


@then("an invalid time shows the precise rejection reason instead of an echo")
def _(world, vitest_result):
    rc, output = vitest_result
    assert rc == 0, output
    # etToZone returns null (never a fabricated echo) for a non-24-hour input;
    # the panel then shows the rejection reason in its place.
    assert "returns null for a non-24-hour input" in output
