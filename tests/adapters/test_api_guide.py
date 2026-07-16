"""GET /guide -- the DOC-01..05 how-it-works tab's backend half (doc 12).

DOC-05: the tab renders from spec/12-how-it-works.md itself (single source,
never a build-time copy that can drift) and reports the RUNNING build's own
spec version alongside the guide's own "describes spec vX.YY" stamp, so the
frontend can banner a mismatch instead of pretending currency. These tests
pin: the real repo spec currently matches (no false-positive banner), an
injected mismatch is detected (fail-first), DOC-05's failure POLARITY --
either version failing to parse reports `version_unknown` so the banner
fails toward SHOWING, never toward false currency (review, 2026-07-15) --
a spec/12 with no "# THE GUIDE" marker refuses honestly rather than
rendering the Rules preamble (DOC-02), and DOC-03's ten-chapter
completeness contract survives the read.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meic.adapters.api.app import create_app
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.event_log import EventLog
from meic.application.persistent_state import PersistentState

REPO_ROOT = Path(__file__).resolve().parents[2]

_README_VERSION_RE = re.compile(r"^- Version:\s*(\d+\.\d+)", re.MULTILINE)


def readme_changelog_head() -> str:
    """The RUNNING build's own spec version, read from the real spec/
    README.md changelog head -- the version-agnostic anchor for the
    real-tree currency pin below (v1.79 ruling; same helper as
    test_api_getting_started.py's, duplicated because tests/ is not an
    importable package)."""
    text = (REPO_ROOT / "spec" / "README.md").read_text(encoding="utf-8-sig")
    match = _README_VERSION_RE.search(text)
    assert match is not None, "spec/README.md changelog head unparseable"
    return match.group(1)

GUIDE_WITH_STAMP = (
    "# 12 -- How It Works\n\n"
    "## Rules\n\n- DOC-01 ... rule commentary that must never render ...\n\n"
    "---\n\n"
    "# THE GUIDE (ratified content, v1.72 -- describes spec v1.72; DOC-05 stamp)\n\n"
    "## 1. What the bot trades\n\nbody\n"
)
README_V199 = (
    "# MEIC Trading Bot\n\n"
    "## Status\n\n"
    "- Version: 1.99 -- 2026-08-01\n"
    "- v1.99 changes: something new that doc 12 hasn't been re-ratified against.\n"
)


def _client_for(tmp_path: Path, guide_text: str, readme_text: str) -> TestClient:
    """A panel wired to a crafted spec/ tree (spec_root override, tests only)."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "12-how-it-works.md").write_text(guide_text, encoding="utf-8")
    (spec_dir / "README.md").write_text(readme_text, encoding="utf-8")
    events = EventLog(config_version="test")
    state = PersistentState(InMemoryStateStore())
    return TestClient(create_app(state, events, spec_root=tmp_path))


@pytest.fixture
def wired():
    events = EventLog(config_version="test")
    state = PersistentState(InMemoryStateStore())
    app = create_app(state, events)
    return TestClient(app)


def test_doc05_serves_the_real_ratified_guide_with_no_mismatch(wired):
    """VERSION-AGNOSTIC real-tree pin (the v1.79 ruling; this test was red
    from v1.73 through v1.78 while the guide's stamp lagged the changelog --
    the mismatch banner working as designed -- and the v1.79 delta pass
    re-ratified the guide current again): the guide's own stamp must EQUAL
    spec/README.md's changelog head, whatever version that reads today, so
    the endpoint must NOT banner against the actual, currently-shipped spec
    tree (a false-positive would be exactly the "pretending currency"
    failure DOC-05 forbids in the opposite direction). The crafted-tree
    fixtures below keep hardcoded, unequal versions to prove the comparison
    isn't a tautology. If a future amendment bumps the README without
    re-stamping the guide, this goes red again -- that red IS the banner
    firing correctly, resolved by the adviser's delta pass, never by editing
    the expectation to match the drift."""
    r = wired.get("/guide")
    assert r.status_code == 200
    body = r.json()
    head = readme_changelog_head()
    assert body["guide_version"] == head
    assert body["running_spec_version"] == head
    assert body["version_mismatch"] is False
    assert body["version_unknown"] is False
    assert body["guide_markdown"].startswith("# THE GUIDE")
    assert "```mermaid" in body["guide_markdown"]


def test_doc03_all_ten_chapters_present(wired):
    """DOC-03's chapter list is the completeness contract -- pin that every
    one of the ten required chapters survives the split-at-heading read."""
    guide = wired.get("/guide").json()["guide_markdown"]
    expected_chapters = [
        "## 1. What the bot trades",
        "## 2. Setting up a day",
        "## 3. The three switches",
        "## 4. What happens at entry time",
        "## 5. The stops",
        "## 6. The watchers",
        "## 7. Manual controls",
        "## 8. When things go wrong",
        "## 9. The dashboard",
        "## 10. The calendar",
    ]
    for chapter in expected_chapters:
        assert chapter in guide, f"missing DOC-03 chapter: {chapter}"


def test_doc05_stamp_mismatch_banners_instead_of_pretending_currency(tmp_path):
    """Fail-first proof of the mismatch path: craft a spec/ tree where the
    guide's stamp (v1.72) disagrees with README's changelog head (v1.99) and
    confirm the endpoint reports version_mismatch=True rather than silently
    reporting the stale guide as current."""
    client = _client_for(tmp_path, GUIDE_WITH_STAMP, README_V199)

    body = client.get("/guide").json()
    assert body["guide_version"] == "1.72"
    assert body["running_spec_version"] == "1.99"
    assert body["version_mismatch"] is True
    assert body["version_unknown"] is False


def test_doc05_matching_stamp_does_not_banner(tmp_path):
    """Sibling of the mismatch test above: same shape, matching versions --
    confirms the comparison isn't a tautology that always fires."""
    client = _client_for(
        tmp_path,
        GUIDE_WITH_STAMP.replace("v1.72", "v2.01").replace("spec v1.72", "spec v2.01"),
        "# MEIC Trading Bot\n\n## Status\n\n- Version: 2.01 -- 2026-08-01\n",
    )

    body = client.get("/guide").json()
    assert body["version_mismatch"] is False
    assert body["version_unknown"] is False


# --- DOC-05 failure polarity (review, 2026-07-15): unparseable => banner ------
# `version_mismatch` alone fails toward FALSE when either version cannot be
# parsed -- a parse failure silently disabling the banner would be the lying-
# gate defect in words. `version_unknown` reports it distinctly and the
# frontend banners on EITHER flag. Both parse-failure sides pinned fail-first.

def test_doc05_missing_guide_stamp_reports_version_unknown(tmp_path):
    """Strip the "describes spec vX.YY" stamp from the guide heading: the
    comparison is unverifiable, so the payload must say so distinctly --
    never a quiet version_mismatch=False that reads as verified currency."""
    client = _client_for(
        tmp_path,
        GUIDE_WITH_STAMP.replace(" -- describes spec v1.72; DOC-05 stamp", ""),
        README_V199,
    )

    body = client.get("/guide").json()
    assert body["guide_version"] is None
    assert body["running_spec_version"] == "1.99"
    assert body["version_unknown"] is True
    assert body["version_mismatch"] is False  # unknown, not a concrete vX-vs-vY


def test_doc05_broken_readme_version_line_reports_version_unknown(tmp_path):
    """Break README's "- Version: X.Y" changelog-head line (the running-
    version source): same polarity -- unverifiable must banner, not pass."""
    client = _client_for(
        tmp_path,
        GUIDE_WITH_STAMP,
        "# MEIC Trading Bot\n\n## Status\n\nVersion line mangled beyond parsing\n",
    )

    body = client.get("/guide").json()
    assert body["guide_version"] == "1.72"
    assert body["running_spec_version"] is None
    assert body["version_unknown"] is True
    assert body["version_mismatch"] is False


def test_doc02_missing_guide_marker_refuses_honestly(tmp_path):
    """A spec/12 with no "# THE GUIDE" marker must NOT fall back to serving
    the whole file -- that would render the Rules preamble (rule IDs,
    ratification commentary) to the operator, a DOC-02 violation. The honest
    answer is the same "guide unavailable" refusal as a missing file."""
    client = _client_for(
        tmp_path,
        "# 12 -- How It Works\n\n## Rules\n\n- DOC-01 ... preamble only, no marker ...\n",
        README_V199,
    )

    r = client.get("/guide")
    assert r.status_code == 500
    assert "guide unavailable" in r.json()["detail"]


def test_doc05_missing_spec_file_is_an_honest_unavailable_not_a_traceback(tmp_path):
    """The whole OSError family (missing file here; permissions/is-a-directory
    take the same except branch) lands as the handled "guide unavailable"
    payload, never an unhandled traceback."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "README.md").write_text(README_V199, encoding="utf-8")
    # no 12-how-it-works.md at all
    events = EventLog(config_version="test")
    state = PersistentState(InMemoryStateStore())
    client = TestClient(create_app(state, events, spec_root=tmp_path))

    r = client.get("/guide")
    assert r.status_code == 500
    assert "guide unavailable" in r.json()["detail"]
