"""CAL-09 (v1.77, doc 11) -- adapters/calendar_sources unit coverage.

Split into three concerns: (1) `common.fetch_text`'s structural domain
allowlist + redirect refusal (the "read-only, unauthenticated ... those
named domains ONLY" mandate, tested with an injected `httpx.MockTransport`
-- no real network, no real server); (2) each source's pure `parse()`
against its own saved fixture (real for FOMC/BEA, CONSTRUCTED-and-flagged
for BLS -- see bls.py's module docstring); (3) each source's `fetch()`
plausibility/empty-parse rejection logic, with `fetch_text` monkeypatched
so no test in this file ever makes a real network call.

No pytest-asyncio in this repo's test stack -- every async call is driven
with `asyncio.run(...)` from a plain sync test function, the same
convention tests/bdd/test_tc_cal_02.py already uses.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from meic.adapters.calendar_sources import bea, bls, fomc
from meic.adapters.calendar_sources.common import WrongHostRefused, fetch_text

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "calendar_sources"


# --- (1) common.fetch_text structural allowlist ------------------------------

def test_fetch_text_refuses_a_url_on_the_wrong_host_before_any_request():
    with pytest.raises(WrongHostRefused):
        asyncio.run(fetch_text("https://evil.example.com/x", allowed_host="www.federalreserve.gov"))


def test_fetch_text_follows_a_redirect_that_stays_on_the_allowed_host():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://www.federalreserve.gov/a":
            return httpx.Response(302, headers={"location": "https://www.federalreserve.gov/b"})
        return httpx.Response(200, text="landed")

    transport = httpx.MockTransport(handler)
    text = asyncio.run(fetch_text("https://www.federalreserve.gov/a",
                                   allowed_host="www.federalreserve.gov", transport=transport))
    assert text == "landed"


def test_fetch_text_refuses_a_redirect_to_a_different_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(WrongHostRefused):
        asyncio.run(fetch_text("https://www.federalreserve.gov/a",
                                allowed_host="www.federalreserve.gov", transport=transport))


def test_fetch_text_raises_on_a_non_200_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(fetch_text("https://www.federalreserve.gov/a",
                                allowed_host="www.federalreserve.gov", transport=transport))


def test_fetch_text_refuses_a_non_https_scheme_before_any_request():
    """Review fix 6 (2026-07-16): https-only, alongside the host allowlist --
    an http:// URL is refused outright even on the right host."""
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request may be sent for a non-https URL")

    transport = httpx.MockTransport(handler)
    with pytest.raises(WrongHostRefused):
        asyncio.run(fetch_text("http://www.federalreserve.gov/a",
                                allowed_host="www.federalreserve.gov", transport=transport))


def test_fetch_text_refuses_a_redirect_downgrade_to_http():
    """Review fix 6: a redirect landing on the RIGHT host but over http://
    is a transport downgrade -- refused, never followed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://www.federalreserve.gov/b"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(WrongHostRefused):
        asyncio.run(fetch_text("https://www.federalreserve.gov/a",
                                allowed_host="www.federalreserve.gov", transport=transport))


def test_fetch_text_resolves_a_relative_redirect_and_follows_it():
    """Review fix 5 (2026-07-16): a RELATIVE Location header is resolved
    against the request URL (urljoin, RFC 3986) and then re-validated --
    the pre-fix code silently re-requested the ORIGINAL url instead of the
    redirect target."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        return httpx.Response(200, text="landed-on-b")

    transport = httpx.MockTransport(handler)
    text = asyncio.run(fetch_text("https://www.federalreserve.gov/a",
                                   allowed_host="www.federalreserve.gov", transport=transport))
    assert text == "landed-on-b"
    # the second request went to the RESOLVED target, not back to /a
    assert seen == ["https://www.federalreserve.gov/a", "https://www.federalreserve.gov/b"]


def test_fetch_text_refuses_a_second_redirect():
    """Review fix 5: one manual, re-validated hop only -- a redirect chain
    is refused rather than walked."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://www.federalreserve.gov/next"})

    transport = httpx.MockTransport(handler)
    with pytest.raises(WrongHostRefused):
        asyncio.run(fetch_text("https://www.federalreserve.gov/a",
                                allowed_host="www.federalreserve.gov", transport=transport))


def test_fetch_text_rejects_an_oversized_response_before_parsing(monkeypatch):
    """Review fix 3 (2026-07-16): a response body over MAX_RESPONSE_BYTES is
    refused before any parser sees it -- counted as a fetch failure by the
    sources (reject-don't-replace), never fed to a regex parser."""
    from meic.adapters.calendar_sources import common
    from meic.adapters.calendar_sources.common import ResponseTooLarge

    monkeypatch.setattr(common, "MAX_RESPONSE_BYTES", 64)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 65)

    transport = httpx.MockTransport(handler)
    with pytest.raises(ResponseTooLarge):
        asyncio.run(fetch_text("https://www.federalreserve.gov/a",
                                allowed_host="www.federalreserve.gov", transport=transport))


# --- (2) pure parse() against saved fixtures ---------------------------------

def test_fomc_parse_reads_the_real_fixture():
    html = (FIXTURES / "fomc_calendars_2026.html").read_text(encoding="utf-8")
    dates = fomc.parse(html, min_year=2026)
    assert "2026-01-28" in dates
    assert len([d for d in dates if d.startswith("2026")]) == 8
    assert all(d.startswith(("2026", "2027")) for d in dates)
    assert fomc._plausible(dates) is True


def test_fomc_parse_skips_notation_vote_and_handles_month_crossing():
    html = (FIXTURES / "fomc_calendars_2026.html").read_text(encoding="utf-8")
    # The real fixture's 2025 section carries a genuine "(notation vote)"
    # entry and several month-crossing ranges (e.g. "30-1") -- widen the
    # floor to prove both are handled, not just skipped-and-invisible.
    dates = fomc.parse(html, min_year=2021)
    assert "2025-08-22" not in dates  # the notation-vote entry names no real decision day
    # Real month-crossing meetings are labelled with a combined month string
    # (e.g. "Jan/Feb"/"30-1" -> decision day Feb 1) -- these must resolve to
    # the SECOND month's date, never be silently dropped (the parser's first
    # cut regex-missed the "/" and dropped every one of these rows outright).
    assert "2023-02-01" in dates    # 2023 "Jan/Feb" meeting, dates "31-1"
    assert "2023-11-01" in dates    # 2023 "Oct/Nov" meeting, dates "31-1"
    assert "2024-05-01" in dates    # 2024 "Apr/May" meeting, dates "30-1"


def test_fomc_decision_date_rejects_unparsable_month():
    assert fomc._decision_date(2026, "Nonsense", "1-2") is None


def test_fomc_plausible_rejects_an_implausible_per_year_count():
    garbage = tuple(f"2026-01-{d:02d}" for d in range(1, 32)) + \
              tuple(f"2026-02-{d:02d}" for d in range(1, 10))  # 40 dates in one year
    assert fomc._plausible(garbage) is False


def test_fomc_plausible_rejects_empty():
    assert fomc._plausible(()) is False


def test_bea_parse_reads_the_real_fixture():
    html = (FIXTURES / "bea_schedule.html").read_text(encoding="utf-8")
    parsed = bea.parse(html)
    assert parsed["GDP"], "expected at least one real GDP release date"
    assert parsed["PCE"], "expected at least one real Personal Income and Outlays (PCE) date"
    assert all(d.startswith("2026-") for d in parsed["GDP"] + parsed["PCE"])


def test_bea_parse_raises_on_unrecognisable_page():
    with pytest.raises(ValueError):
        bea.parse("<html><body>nothing here</body></html>")


def test_bls_parse_reads_the_constructed_fixture():
    html = (FIXTURES / "bls_2026_constructed.html").read_text(encoding="utf-8")
    parsed = bls.parse(html)
    assert parsed["CPI"] == ("2026-01-13", "2026-02-11", "2026-03-11")
    assert parsed["PPI"] == ("2026-01-14", "2026-02-18", "2026-03-18")
    assert parsed["NFP"] == ("2026-01-09", "2026-02-06", "2026-03-06")


def test_bls_parse_raises_on_unrecognisable_page():
    with pytest.raises(ValueError):
        bls.parse("<html><body>nothing here</body></html>")


# --- (3) fetch() plausibility/empty-parse rejection, fetch_text stubbed -----

def test_fomc_source_fetch_rejects_on_fetch_failure(monkeypatch):
    async def _boom(url, *, allowed_host, timeout=15.0, transport=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(fomc, "fetch_text", _boom)
    results = asyncio.run(fomc.FomcSource().fetch())
    assert len(results) == 1 and results[0].ok is False
    assert results[0].reason.startswith("fetch_failed:")


def test_fomc_source_fetch_rejects_an_implausible_parse(monkeypatch):
    garbage_html = "<h4><a id=\"1\">2026 FOMC Meetings</a></h4>" + "".join(
        f'<div class="row fomc-meeting"><div class="fomc-meeting__month"><strong>January</strong>'
        f'</div><div class="fomc-meeting__date">{d}</div></div>' for d in range(1, 32))

    async def _fake(url, *, allowed_host, timeout=15.0, transport=None):
        return garbage_html

    monkeypatch.setattr(fomc, "fetch_text", _fake)
    results = asyncio.run(fomc.FomcSource(min_year_fn=lambda: 2026).fetch())
    assert len(results) == 1 and results[0].ok is False
    assert results[0].reason.startswith("implausible_count:")


def test_fomc_source_fetch_accepts_a_clean_real_fixture(monkeypatch):
    html = (FIXTURES / "fomc_calendars_2026.html").read_text(encoding="utf-8")

    async def _fake(url, *, allowed_host, timeout=15.0, transport=None):
        return html

    monkeypatch.setattr(fomc, "fetch_text", _fake)
    results = asyncio.run(fomc.FomcSource(min_year_fn=lambda: 2026).fetch())
    assert len(results) == 1 and results[0].ok is True
    assert results[0].dates


def test_bea_source_fetch_rejects_empty_category_independently(monkeypatch):
    # A page with a recognisable year header and GDP rows but NO PCE rows at
    # all must reject PCE while still accepting GDP -- independent per-
    # category verdicts, never one failure dragging down the other.
    html = ('<table id="release-schedule-table"><thead><tr><th>Year 2026</th></tr></thead>'
            '<tbody><tr class="scheduled-releases-type-press">'
            '<td><div class="release-date">July 30</div></td>'
            '<td class="release-title">GDP (Advance Estimate), 2nd Quarter 2026</td>'
            '</tr></tbody></table>')

    async def _fake(url, *, allowed_host, timeout=15.0, transport=None):
        return html

    monkeypatch.setattr(bea, "fetch_text", _fake)
    results = asyncio.run(bea.BeaSource().fetch())
    by_cat = {r.category: r for r in results}
    assert by_cat["GDP"].ok is True and by_cat["GDP"].dates == ("2026-07-30",)
    assert by_cat["PCE"].ok is False and by_cat["PCE"].reason == "parse_empty"


def test_bls_source_fetch_rejects_on_fetch_failure_for_every_category(monkeypatch):
    async def _boom(url, *, allowed_host, timeout=15.0, transport=None):
        raise RuntimeError("403 Access Denied")

    monkeypatch.setattr(bls, "fetch_text", _boom)
    results = asyncio.run(bls.BlsSource(year_fn=lambda: 2026).fetch())
    assert {r.category for r in results} == {"CPI", "PPI", "NFP"}
    assert all(not r.ok and r.reason.startswith("fetch_failed:") for r in results)
