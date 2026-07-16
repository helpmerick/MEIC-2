"""GET /getting-started -- the DOC-06/UI-32 fifth tab's backend half (doc 12,
slice 6; template contract as amended by the v1.79 live-only ruling).

DOC-06: the Getting-started tab renders variable NAMES and where-to-obtain
guidance from spec/12-how-it-works.md's own "# GETTING STARTED" ratified
section -- NEVER a current value, password, token, or secret. The structural
guarantee pinned here is the strongest one available: the served payload is
byte-for-byte a substring of the hash-locked spec file's own text (the lock
proves that text contains no secrets), and the endpoint never opens a .env
file (pinned with a planted-sentinel test below, not just by inspection).

The two-section discipline (introduced v1.78) is also pinned here: spec/12
carries TWO independently-stamped ratified sections, and each endpoint
banners against ITS OWN section's stamp -- one section's stamp must never
leak into the other's banner, and neither section's content may bleed into
the other's payload (the pre-slice-6 /guide sliced to end-of-file, which
would have silently swallowed this new section into the how-it-works tab;
the section-boundary extract fixed that and is pinned in both directions
below). The REAL-tree tests are version-agnostic (each section's stamp must
equal the README changelog head, whatever that reads today); the CRAFTED
trees keep hardcoded versions on purpose -- they pin the comparison
mechanism itself and prove it is not a tautology.
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
    README.md changelog head. The real-tree stamp tests below assert against
    THIS instead of a hardcoded number, so a routine ratification bump can
    never go stale in a test name or assertion again -- the honest pin is
    "the section's own stamp equals whatever the changelog head reads
    today", i.e. the section is CURRENT. When a future amendment bumps the
    README without re-stamping the section, that test goes red -- which is
    the design working (the tab would be bannering a real mismatch), not a
    stale expectation."""
    text = (REPO_ROOT / "spec" / "README.md").read_text(encoding="utf-8-sig")
    match = _README_VERSION_RE.search(text)
    assert match is not None, "spec/README.md changelog head unparseable"
    return match.group(1)

# A crafted two-section spec/12 (Rules preamble, then "# THE GUIDE" stamped
# v1.72, then "# GETTING STARTED" stamped v1.78, with the same "---"
# separators the real file uses between sections). The versions here are
# DELIBERATELY hardcoded and deliberately different: these fixtures pin the
# per-section stamp-comparison mechanism (mismatch fires, sections banner
# independently), which needs known, unequal values -- they do not track the
# real tree's current ratification.
TWO_SECTION_SPEC = (
    "# 12 -- How It Works\n\n"
    "## Rules\n\n- DOC-01 ... rule commentary that must never render ...\n\n"
    "---\n\n"
    "# THE GUIDE (ratified content, v1.72 -- describes spec v1.72; DOC-05 stamp)\n\n"
    "## 1. What the bot trades\n\nguide body\n\n"
    "---\n\n"
    "---\n\n"
    "# GETTING STARTED (ratified content, v1.78 -- describes spec v1.78 and the "
    "build's true run procedure; DOC-05 stamp)\n\n"
    "## 1. Prerequisites, and how this build actually runs\n\nsection body\n"
)
README_V178 = (
    "# MEIC Trading Bot\n\n## Status\n\n"
    "- Version: 1.78 -- 2026-07-16\n"
    "- v1.78 changes: DOC-06 Getting Started content.\n"
)
README_V199 = (
    "# MEIC Trading Bot\n\n## Status\n\n"
    "- Version: 1.99 -- 2026-08-01\n"
    "- v1.99 changes: something newer than either section's stamp.\n"
)

# DOC-06's annotated template names -- every one must render as a NAME.
# v1.79 (live-only ruling): the three TT_CERT_* literal names were REMOVED
# from the template and the DOC-06 contract -- the tab documents only the
# production credentials a live operator actually enters. (The bare
# wildcard string "TT_CERT_*" legitimately survives once in the ratified
# prose, inside the code-verified "appears nowhere else in production code"
# note -- so its ABSENCE is not asserted, only the three full names are no
# longer part of the expected-name contract.)
TEMPLATE_NAMES = [
    "MEIC_USER_PASSWORD",
    "TT_PROD_PROVIDER_SECRET", "TT_PROD_REFRESH_TOKEN", "TT_PROD_ACCOUNT",
    "MEIC_LIVE_IS_TEST", "MEIC_ALLOW_PRODUCTION", "MEIC_DATA_DIR",
]
# Value-shaped content the payload must NEVER contain (calibrated against the
# real ratified section, which contains neither): (1) any of the template
# names paired with a value via = or :, (2) any token-shaped run -- 28+
# consecutive credential-alphabet chars (real provider secrets / refresh
# tokens are 32+ alnum; the longest legitimate run in the ratified prose is
# well under 28).
NAME_VALUE_PAIRING = re.compile(
    r"(TT_CERT_\w+|TT_PROD_\w+|MEIC_USER_PASSWORD|MEIC_LIVE_IS_TEST"
    r"|MEIC_ALLOW_PRODUCTION|MEIC_DATA_DIR)\s*[=:]\s*\S+")
TOKEN_SHAPED = re.compile(r"[A-Za-z0-9+/_\-]{28,}")


def _client_for(tmp_path: Path, spec_text: str, readme_text: str) -> TestClient:
    """A panel wired to a crafted spec/ tree (spec_root override, tests only)."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "12-how-it-works.md").write_text(spec_text, encoding="utf-8")
    (spec_dir / "README.md").write_text(readme_text, encoding="utf-8")
    events = EventLog(config_version="test")
    state = PersistentState(InMemoryStateStore())
    return TestClient(create_app(state, events, spec_root=tmp_path))


@pytest.fixture
def wired():
    """A panel wired to the REAL repo spec/ tree."""
    events = EventLog(config_version="test")
    state = PersistentState(InMemoryStateStore())
    return TestClient(create_app(state, events))


def test_doc06_serves_the_real_ratified_section_stamped_current(wired):
    """VERSION-AGNOSTIC real-tree pin (the v1.79 ruling): the real "# GETTING
    STARTED" section's own stamp must EQUAL spec/README.md's changelog head
    -- whatever version that reads today -- so this endpoint must not
    banner. Its own stamp, its own comparison; the crafted-tree fixtures
    below keep hardcoded, unequal versions to prove the comparison isn't a
    tautology."""
    r = wired.get("/getting-started")
    assert r.status_code == 200
    body = r.json()
    head = readme_changelog_head()
    assert body["getting_started_version"] == head
    assert body["running_spec_version"] == head
    assert body["version_mismatch"] is False
    assert body["version_unknown"] is False
    assert body["getting_started_markdown"].startswith("# GETTING STARTED")


def test_doc06_all_five_sections_present(wired):
    """DOC-06's five-section list is the completeness contract."""
    md = wired.get("/getting-started").json()["getting_started_markdown"]
    expected = [
        "## 1. Prerequisites, and how this build actually runs",
        "## 2. The `.env` file",
        "## 3. The numbers live mode refuses to trade without",
        "## 4. The paper-first first-run sequence",
        "## 5. Going live",
    ]
    for section in expected:
        assert section in md, f"missing DOC-06 section: {section}"


def test_doc06_payload_is_exactly_the_hash_locked_spec_text(wired):
    """The structural half of the no-secret-leak pin: the served markdown is
    byte-for-byte a substring of spec/12-how-it-works.md -- the endpoint can
    only ever serve what the hash-locked spec contains, and the lock proves
    that text holds names and guidance, never a live value."""
    md = wired.get("/getting-started").json()["getting_started_markdown"]
    full_text = (REPO_ROOT / "spec" / "12-how-it-works.md").read_text(encoding="utf-8-sig")
    assert md in full_text


def test_doc06_names_render_but_no_value_shaped_content_ever(wired):
    """The content half of the no-secret-leak pin (TC-DOC-01 scenario 3,
    backend side): every template variable NAME appears literally in the
    payload, while NOTHING value-shaped does -- no NAME=value/NAME: value
    pairing, no token-shaped 28+ char credential-alphabet run anywhere."""
    md = wired.get("/getting-started").json()["getting_started_markdown"]
    for name in TEMPLATE_NAMES:
        assert name in md, f"template variable name missing from the tab: {name}"
    assert NAME_VALUE_PAIRING.search(md) is None, "a template name is paired with a value"
    assert TOKEN_SHAPED.search(md) is None, "token-shaped run in the payload"


def test_doc06_endpoint_never_reads_a_dotenv_file(tmp_path):
    """Plant a .env with a sentinel secret in the crafted tree (both at the
    spec root and inside spec/); the sentinel must never appear in either
    endpoint's payload. The tab's source is the spec text alone -- a build
    that ever merged live env values into this payload would leak here."""
    sentinel = "PLANTED-SENTINEL-SECRET-VALUE-12345"
    (tmp_path / ".env").write_text(
        f"MEIC_USER_PASSWORD={sentinel}\nTT_PROD_REFRESH_TOKEN={sentinel}\n",
        encoding="utf-8")
    client = _client_for(tmp_path, TWO_SECTION_SPEC, README_V178)
    (tmp_path / "spec" / ".env").write_text(
        f"MEIC_USER_PASSWORD={sentinel}\n", encoding="utf-8")

    for route in ("/getting-started", "/guide"):
        body = client.get(route).json()
        assert sentinel not in str(body), f"{route} leaked a planted .env value"


# --- the v1.78 two-section split: own stamps, clean boundaries ----------------

def test_doc05_two_sections_banner_independently_against_their_own_stamps(tmp_path):
    """The ruled two-stamp discipline: with the guide stamped v1.72 and
    getting-started stamped v1.78 against a running v1.78 spec, /guide
    banners its mismatch while /getting-started reports clean -- one
    section's stamp never bleeds into the other's banner."""
    client = _client_for(tmp_path, TWO_SECTION_SPEC, README_V178)

    guide = client.get("/guide").json()
    assert guide["guide_version"] == "1.72"
    assert guide["version_mismatch"] is True

    gs = client.get("/getting-started").json()
    assert gs["getting_started_version"] == "1.78"
    assert gs["version_mismatch"] is False
    assert gs["version_unknown"] is False


def test_doc05_guide_payload_no_longer_swallows_the_second_section(tmp_path):
    """The boundary fix, pinned in the direction that actually broke: before
    slice 6, /guide sliced from "# THE GUIDE" to end-of-file, which would
    have served the whole GETTING STARTED section inside the how-it-works
    tab. Each payload now holds exactly its own section."""
    client = _client_for(tmp_path, TWO_SECTION_SPEC, README_V178)

    guide_md = client.get("/guide").json()["guide_markdown"]
    assert "# GETTING STARTED" not in guide_md
    assert "Prerequisites" not in guide_md

    gs_md = client.get("/getting-started").json()["getting_started_markdown"]
    assert "# THE GUIDE" not in gs_md
    assert "What the bot trades" not in gs_md
    # Neither payload renders the Rules preamble (DOC-02).
    assert "rule commentary that must never render" not in guide_md
    assert "rule commentary that must never render" not in gs_md


def test_doc06_real_tree_boundary_guide_excludes_getting_started(wired):
    """Same boundary pin against the REAL spec tree: the how-it-works tab's
    payload ends at the guide; the fifth tab's content is not inside it."""
    guide_md = wired.get("/guide").json()["guide_markdown"]
    assert "# GETTING STARTED" not in guide_md
    gs_md = wired.get("/getting-started").json()["getting_started_markdown"]
    assert "# THE GUIDE" not in gs_md


# --- DOC-05 failure polarity, mirrored from /guide's own pins -----------------

def test_doc06_stamp_mismatch_banners_instead_of_pretending_currency(tmp_path):
    """Fail-first proof of THIS section's mismatch path: running spec v1.99
    against the v1.78 stamp must report version_mismatch=True."""
    client = _client_for(tmp_path, TWO_SECTION_SPEC, README_V199)

    body = client.get("/getting-started").json()
    assert body["getting_started_version"] == "1.78"
    assert body["running_spec_version"] == "1.99"
    assert body["version_mismatch"] is True
    assert body["version_unknown"] is False


def test_doc06_missing_stamp_reports_version_unknown(tmp_path):
    """Strip the section's own "describes spec vX.YY" stamp: the comparison
    is unverifiable and must say so distinctly (fails toward SHOWING) --
    never a quiet version_mismatch=False that reads as verified currency.
    The sibling guide section's intact v1.72 stamp must NOT be picked up as
    a substitute (the bleed this slice guards against)."""
    no_stamp = TWO_SECTION_SPEC.replace(
        "v1.78 -- describes spec v1.78 and the build's true run procedure; DOC-05 stamp",
        "ratified, stampless")
    client = _client_for(tmp_path, no_stamp, README_V178)

    body = client.get("/getting-started").json()
    assert body["getting_started_version"] is None
    assert body["version_unknown"] is True
    assert body["version_mismatch"] is False


def test_doc06_broken_readme_version_line_reports_version_unknown(tmp_path):
    """Break README's changelog-head line: same polarity as /guide's pin."""
    client = _client_for(
        tmp_path, TWO_SECTION_SPEC,
        "# MEIC Trading Bot\n\n## Status\n\nVersion line mangled beyond parsing\n")

    body = client.get("/getting-started").json()
    assert body["getting_started_version"] == "1.78"
    assert body["running_spec_version"] is None
    assert body["version_unknown"] is True
    assert body["version_mismatch"] is False


def test_doc02_missing_marker_refuses_honestly(tmp_path):
    """A spec/12 with no "# GETTING STARTED" marker must NOT fall back to
    serving the whole file or the guide's own content -- the honest answer
    is the same "unavailable" refusal as a missing file (DOC-02)."""
    guide_only = (
        "# 12 -- How It Works\n\n## Rules\n\n- DOC-01 ... preamble ...\n\n---\n\n"
        "# THE GUIDE (ratified content, v1.72 -- describes spec v1.72; DOC-05 stamp)\n\n"
        "## 1. What the bot trades\n\nbody\n")
    client = _client_for(tmp_path, guide_only, README_V178)

    r = client.get("/getting-started")
    assert r.status_code == 500
    assert "getting started unavailable" in r.json()["detail"]
    # And the guide endpoint keeps working -- one section's absence never
    # takes down the other's tab.
    assert client.get("/guide").status_code == 200


def test_doc06_missing_spec_file_is_an_honest_unavailable_not_a_traceback(tmp_path):
    """The whole OSError family lands as the handled "unavailable" payload,
    never an unhandled traceback."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "README.md").write_text(README_V178, encoding="utf-8")
    # no 12-how-it-works.md at all
    events = EventLog(config_version="test")
    state = PersistentState(InMemoryStateStore())
    client = TestClient(create_app(state, events, spec_root=tmp_path))

    r = client.get("/getting-started")
    assert r.status_code == 500
    assert "getting started unavailable" in r.json()["detail"]
