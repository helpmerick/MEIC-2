"""Hand-written step definitions for TC-UI-09 — UI-31 (v1.73, queue slice 5):
sticky ET date separators, per-row ET wall-clock times, hover/focus/tap
tooltips explaining every renderable event in doc-12 vocabulary, and the
completeness gate that fails, naming the event type, if one is ever added
with no explanation.

BINDING STRATEGY (same split every UI-flavoured TC here uses, e.g.
test_tc_ui_07.py): behaviour that only exists as rendered React/DOM state
(sticky positioning, per-row times, the actual tooltip interaction) is bound
to the REAL vitest suites via the session-scoped `vitest_ui09_result` fixture
(tests/bdd/conftest.py) — never a Python re-implementation. Structural
invariants that hold regardless of any one test run (the day-separator loop
can structurally never emit a header without an accompanying item; the
vocabulary text traces back to the ratified doc-12 source) are instead pinned
by reading the real source files directly, the same technique test_tc_ui_07.py
uses for UI-23a's "no geolocation anywhere" clause.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then

scenarios("../features/TC-UI-09.feature")


@pytest.fixture
def world():
    return {}

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
APP_PY = Path(__file__).resolve().parents[2] / "backend" / "src" / "meic" / "adapters" / "api" / "app.py"


def _renderable_event_types() -> list[str]:
    """Mirrors activityVocabulary.test.ts's own extraction exactly (same
    regex, same source file) — the completeness gate's real authority."""
    src = APP_PY.read_text(encoding="utf-8")
    m = re.search(r"table:\s*dict\[str,\s*tuple\[str,\s*str\]\]\s*=\s*\{([\s\S]*?)\n\s*\}", src)
    assert m, "could not locate _describe's event `table` dict in adapters/api/app.py"
    keys = re.findall(r'"([A-Za-z0-9_]+)":', m.group(1))
    assert keys, "matched the _describe table block but extracted zero event-type keys"
    return keys


# --- Scenario: Days are visually separated, never continuous ----------------

@given("activity spanning 2026-07-13 and 2026-07-14")
def _(world):
    # The real fixture proving this exists in ActivityFeed.test.tsx's own
    # "renders a separator row before the first item of each new day,
    # newest-first" test (day-separator feature, pre-existing this slice) —
    # bound below via the real vitest run, never re-implemented here.
    world["days"] = ("2026-07-13", "2026-07-14")


@then("a sticky ET date header renders between the two days")
def _(vitest_ui09_result):
    rc, output = vitest_ui09_result
    assert rc == 0, output
    # The header renders at all, between two days:
    assert "renders a separator row before the first item of each new day, newest-first" in output
    # ...and is positioned STICKY within the feed's own scroll container:
    assert "positions the day separator sticky within the feed's own scroll container" in output


@then("each row shows its own ET time")
def _(vitest_ui09_result):
    rc, output = vitest_ui09_result
    assert rc == 0, output
    assert "shows the row's own ET wall-clock time, derived from its `at` instant via instantToZone" in output


@then("days without activity render no header")
def _():
    # Structural guarantee, true independent of any one test run: the
    # day-separator loop (activityDays.ts groupActivityByDay) only ever
    # pushes a `{kind: "separator"}` row INSIDE the same iteration that also
    # pushes the item that day belongs to -- there is no code path that
    # iterates calendar days independently of actual activity items, so a
    # day with zero activity items structurally cannot produce a header.
    src = (FRONTEND_SRC / "activityDays.ts").read_text(encoding="utf-8")
    loop = re.search(
        r"for \(const \{ item, day \} of sorted\) \{\s*"
        r"if \(day !== null && day !== lastDay\) \{\s*"
        r'rows\.push\(\{ kind: "separator", day, label: formatDaySeparator\(day\) \}\);',
        src,
    )
    assert loop, (
        "expected groupActivityByDay's single walk over `sorted` items to be the "
        "ONLY place a separator row is ever pushed -- a header can never be "
        "invented for a day with no corresponding activity item"
    )
    # And confirm there is no second, independent day-emission path (e.g. a
    # calendar walk) anywhere else in the same file -- count actual
    # CONSTRUCTIONS of a separator row (`rows.push({ kind: "separator" ...`),
    # not the `ActivityRow` type union's own literal-type declaration above.
    assert src.count('rows.push({ kind: "separator"') == 1, \
        "expected exactly one place in activityDays.ts that ever constructs a separator row"


# --- Scenario: Every activity explains itself on hover -----------------------

@given("any rendered activity row")
def _(world):
    # Every event type the feed can render is guaranteed (by the completeness
    # gate below) to carry a vocabulary entry -- so "any" row is representative.
    # LongSold is used as the concrete example in ActivityFeed.test.tsx.
    world["example_type"] = "LongSold"


@then("a styled focus- and tap-capable tooltip explains the event in plain English")
def _(vitest_ui09_result):
    rc, output = vitest_ui09_result
    assert rc == 0, output
    assert "a known event type gets a styled, focus- and tap-capable tooltip -- never a native title" in output
    # The Tooltip component itself is the ONE styled, focus+tap-capable
    # disclosure primitive (v1.63 standard) -- ActivityFeed reuses it rather
    # than inventing a second implementation.
    feed_src = (FRONTEND_SRC / "components" / "ActivityFeed.tsx").read_text(encoding="utf-8")
    assert 'import { Tooltip } from "./Tooltip";' in feed_src


@then("the wording uses the doc-12 chapter vocabulary")
def _():
    # The LEX definition is required (by commission) to be sourced verbatim
    # from the ratified delta passage, cited in a comment -- and to share its
    # core phrasing ("long exit", "walking the price down") with spec/12's
    # own Chapter 6 prose, so the feed and the guide can never fork.
    vocab_src = (FRONTEND_SRC / "activityVocabulary.ts").read_text(encoding="utf-8")
    assert "DRAFT-DOC-12-DELTA-v1.77-2026-07-15.md" in vocab_src, \
        "the LEX tooltip must cite its ratified source passage in a comment"
    assert "long exit" in vocab_src.lower()

    how_it_works = (Path(__file__).resolve().parents[2] / "spec" / "12-how-it-works.md").read_text(encoding="utf-8")
    assert "walking the price down" in how_it_works, \
        "expected phrase missing from spec/12 -- has Chapter 6's ladder wording changed?"
    assert "walking the price down" in vocab_src, \
        "the LEX tooltip's wording has drifted from the guide's own Chapter 6 phrasing"


@then("no native title attribute carries the explanation")
def _(vitest_ui09_result):
    rc, output = vitest_ui09_result
    assert rc == 0, output
    assert "a known event type gets a styled, focus- and tap-capable tooltip -- never a native title" in output
    # Belt-and-braces: the shared Tooltip component itself never renders a
    # native `title` attribute anywhere on its trigger or bubble (v1.63).
    tooltip_src = (FRONTEND_SRC / "components" / "Tooltip.tsx").read_text(encoding="utf-8")
    assert "title=" not in tooltip_src


# --- Scenario: An unexplained event type is a test failure ------------------

@given("an event type renderable by the feed with no explanation entry")
def _(world):
    world["event_types"] = _renderable_event_types()


@then("the suite fails naming the event type")
def _(world, vitest_ui09_result):
    # In the PASSING state (every real event type IS explained), this proves
    # the negative: no backend-renderable event type is missing a vocabulary
    # entry right now. The gate's OWN assertion message (activityVocabulary.
    # test.ts) is what names the offending type the moment one goes missing —
    # `expect(missing, ...).toEqual([])` renders the message we check for
    # below, so this Then step pins both halves: the gate runs for real, AND
    # its failure message would name the type, never fail silently/generically.
    vocab_src = (FRONTEND_SRC / "activityVocabulary.ts").read_text(encoding="utf-8")
    missing = [t for t in world["event_types"] if f'"{t}"' not in vocab_src and f"{t}:" not in vocab_src]
    assert missing == [], f"backend event type(s) with no vocabulary entry: {missing}"

    rc, output = vitest_ui09_result
    assert rc == 0, output
    assert "every event type the backend can render has a plain-English tooltip explanation" in output
    # The gate's failure message, read straight from its own source, would
    # name the event type -- never a bare boolean/generic failure.
    gate_src = (FRONTEND_SRC / "activityVocabulary.test.ts").read_text(encoding="utf-8")
    assert "no tooltip explanation:" in gate_src
