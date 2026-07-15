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


@then("the SPA's top-level tabs are exactly Trading, Results, Calendar, How it works")
def _(vitest_doc_result):
    rc, output = vitest_doc_result
    assert rc == 0, output
    assert "shows exactly the four nav tabs, in order" in output
