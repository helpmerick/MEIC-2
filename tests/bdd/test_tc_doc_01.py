"""TC-DOC-01 -- the How-it-works tab (doc 12, DOC-01..05, v1.72 slice 4).

Both scenarios in TC-DOC-01.feature pin FRONTEND behaviour: the guide's
DOC-05 single-source rendering (version stamp, mismatch banner, DOC-03
chapter completeness, no trading controls) lives in HowItWorksPage.tsx, and
the four-tab nav lives in App.tsx's route list (already exercised by
App.test.tsx's existing "nav" describe block). Both are bound here via the
real vitest suite (`vitest_doc_result`, tests/bdd/conftest.py) -- never a
tautology: a regression in either file fails its own vitest test, the
fixture's returncode goes non-zero, and every clause below fails with it.
Same dual-half binding strategy as TC-CAL-01/02 (`vitest_cal_result`) and
TC-UI-05/06/07 (`vitest_result`/`vitest_ui07_result`).

The backend half of DOC-05 -- GET /guide's own version-stamp parsing and
stamped-vs-running comparison -- is pinned directly (not through this dual-
half indirection) in tests/adapters/test_api_guide.py, including a fail-
first proof of the mismatch path via a crafted spec/ tree.

DOC-05 zoom clause (RPT-12/DOC-05 timeline+diagrams rebuild, v1.77, queue
slice 2+3): the flowchart's click-to-full-screen-pan/zoom behaviour is bound
below the same way, against HowItWorksPage.test.tsx's own dedicated test
("the master flowchart is clickable..."). The shared ZoomableFigure
component's OWN generic pan/zoom mechanics (drag, wheel, pinch, +/-, reset,
Esc) are pinned once in frontend/src/components/ZoomableFigure.test.tsx, not
re-verified through this indirection -- this scenario only pins that the
flowchart is WIRED to it.

DOC-06/UI-32 (doc 12 slice 6, v1.78): the remaining two scenarios --
five-tab nav with "Getting started", and the no-secret-leak contract -- are
now bound the same way, against GettingStartedPage.test.tsx and App.test.tsx
(both in the `vitest_doc_result` run). Their backend halves (GET
/getting-started's own v1.78 stamp parse, the two-section split of spec/12,
the payload-is-exactly-the-hash-locked-spec-text structural secret pin, and
the planted-.env-sentinel test) are pinned directly in
tests/adapters/test_api_getting_started.py.
"""
from __future__ import annotations

from pytest_bdd import given, scenarios, then

scenarios("../features/TC-DOC-01.feature")


@given("the how-it-works tab")
def _():
    """No setup needed here -- the vitest fixture below renders the real
    HowItWorksPage component (and the real App nav) itself; this step only
    names the scenario's subject for readability."""


@then("it renders doc 12's content as single source stamped with the spec version it describes")
def _(vitest_doc_result):
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert "renders the guide fetched from GET /guide, stamped with its own version" in output


@then("a stamped-vs-running version mismatch renders a banner, never silent currency")
def _(vitest_doc_result):
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert ("banners a stamped-vs-running version mismatch instead of pretending "
            "currency (DOC-05)" in output)
    assert "does not banner when the stamp matches the running build (not a tautology)" in output
    # DOC-05 failure polarity (review, 2026-07-15): "never silent currency"
    # includes the UNVERIFIABLE case -- either version failing to parse must
    # banner "cannot verify", not silently read as current. The backend halves
    # of the same polarity are pinned in tests/adapters/test_api_guide.py.
    assert ("banners an UNPARSEABLE guide stamp as 'cannot verify' — fails toward "
            "showing (DOC-05)" in output)
    assert "banners an unreadable RUNNING spec version as 'cannot verify' too (DOC-05)" in output


@then("the master flowchart is clickable to a full-screen pannable zoomable view (v1.77)")
def _(vitest_doc_result):
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert ("the master flowchart is clickable to a full-screen pannable zoomable view "
            "(DOC-05, v1.77)" in output)


@then("every DOC-03 chapter is present (the completeness contract)")
def _(vitest_doc_result):
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert "every DOC-03 chapter present in the fixture appears as its own heading" in output


@then("the tab carries no trading controls")
def _(vitest_doc_result):
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert "carries no trading controls (DOC-05 read-only tab)" in output


@then("the SPA's top-level tabs are exactly Trading, Results, Calendar, How it works, Getting started")
def _(vitest_doc_result):
    """UI-32 (v1.75 operator commission, slice 6): the nav is exactly FIVE
    tabs in the ruled order -- App.test.tsx's nav test asserts the full
    ordered list (an extra, missing, or reordered tab fails it), and the
    dedicated click-through test pins that the fifth tab renders the real
    GettingStartedPage with its own v1.78 stamp."""
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert "shows exactly the five nav tabs, in order" in output
    assert ("clicking Getting started renders the ratified fifth tab with its OWN stamp "
            "(DOC-06/UI-32)" in output)


# --- TC-DOC-01 scenario 3: "Getting-started never leaks a secret" ------------
# DOC-06/UI-32 (doc 12 slice 6, v1.78). Frontend halves bound below through
# GettingStartedPage.test.tsx via the same vitest fixture; the backend halves
# -- the served payload being byte-for-byte the hash-locked spec section's
# text (the structural no-secret guarantee: the endpoint can only serve what
# the spec lock proves contains no secrets), the planted-.env-sentinel never
# leaking into either doc payload, and the two-section stamp independence --
# are pinned directly in tests/adapters/test_api_getting_started.py.


@then("the tab renders variable NAMES and explanations only")
def _(vitest_doc_result):
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert "renders variable NAMES and where-to-obtain guidance only (DOC-06/UI-32)" in output


@then("no current env value, password, token, or secret ever renders anywhere in it")
def _(vitest_doc_result):
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert "never renders a value-shaped secret anywhere in the tab (DOC-06/UI-32)" in output


@then("all five DOC-06 sections are present (the completeness contract)")
def _(vitest_doc_result):
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert "all five DOC-06 sections are present (the completeness contract)" in output
