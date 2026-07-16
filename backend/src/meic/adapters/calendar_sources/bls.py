"""BlsSource -- CAL-09 tier-1 CPI/PPI/Employment-Situation (NFP) fetch
(bls.gov release schedule).

*** LIVE-FORMAT VERIFICATION OUTSTANDING -- flagged, not pretended. ***

bls.gov's own edge bot-defense (Akamai) returned a hard "Access Denied ...
bot activity ... prohibited" 403 to EVERY plain httpx GET attempted against
https://www.bls.gov/schedule/news_release/<year>_sched.htm during this
slice's implementation (2026-07-16, verified against the real live site,
not assumed) -- see `tests/fixtures/calendar_sources/bls_2026_constructed
.html`'s own header comment for the exact response body captured. No
header-spoofing or bot-evasion technique was attempted: CAL-09 mandates a
read-only, unauthenticated, HONEST fetch -- evading a site's own access
policy would not be that, whatever the outcome.

This module's parser is therefore built against a CONSTRUCTED fixture
(clearly marked as such), reflecting BLS's publicly documented plain-table
release-schedule layout, NOT a byte-for-byte capture of the live page --
unlike fomc.py/bea.py, whose fixtures ARE real, trimmed captures. Until an
operator confirms a working fetch path (a different endpoint, a permitted
access arrangement, or accepting this source stays rejected in favour of
the manual paste fallback for CPI/PPI/NFP), CAL-09's own safety rails are
exactly what make this survivable: a rejected fetch alerts and never
touches existing data (rule 1), and after `cal_refresh_fail_alert_days`
consecutive rejections raises the persistent alert (rule 4) -- entries
stay ungated regardless (CAL-07). Reported to the operator, not silently
worked around.

Only the CURRENT year's schedule page is fetched (one URL per year on this
site); a next-year lookahead is a future enhancement, not built here --
flagged, not silently omitted."""
from __future__ import annotations

import datetime as _dt
import re

from meic.adapters.calendar_sources.common import fetch_text
from meic.application.calendar_refresh import CategoryFetch

_HOST = "www.bls.gov"

_ROW_RE = re.compile(r'<tr>\s*<td>.*?<a[^>]*>([^<]+)</a>.*?</td>\s*<td>([^<]+)</td>', re.S)
_DATE_RE = re.compile(r'^\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*$')

_CATEGORY_KEYWORDS = (
    ("Consumer Price Index", "CPI"),
    ("Producer Price Index", "PPI"),
    ("Employment Situation", "NFP"),
)

# No per-category band is named in CAL-09's ratified text beyond FOMC's own
# "6-10/yr" example -- IMPLEMENTATION DECISION, flagged: CPI/PPI/NFP are
# each monthly (~12/yr); this generous upper-only cap exists purely to
# catch a corrupted/garbage parse, mirroring bea.py's own reasoning.
_MAX_PER_CATEGORY = 20


def _url_for_year(year: int) -> str:
    return f"https://www.bls.gov/schedule/news_release/{year}_sched.htm"


def parse(html: str) -> dict[str, tuple[str, ...]]:
    """Pure parse (unit-tested against the CONSTRUCTED fixture only -- see
    this module's docstring). Raises ValueError if no recognised release
    row is found at all -- the caller treats that as a rejected fetch."""
    out: dict[str, set[str]] = {"CPI": set(), "PPI": set(), "NFP": set()}
    matched_any = False
    for m in _ROW_RE.finditer(html):
        name, date_str = m.group(1).strip(), m.group(2).strip()
        category = next((c for kw, c in _CATEGORY_KEYWORDS if kw in name), None)
        if category is None:
            continue
        d = _DATE_RE.match(date_str)
        if not d:
            continue
        month, day, year = int(d.group(1)), int(d.group(2)), int(d.group(3))
        try:
            iso = _dt.date(year, month, day).isoformat()  # validates a REAL calendar date
        except ValueError:
            continue
        out[category].add(iso)
        matched_any = True
    if not matched_any:
        raise ValueError("no recognised BLS release rows found -- page structure unrecognised")
    return {k: tuple(sorted(v)) for k, v in out.items()}


class BlsSource:
    categories = ("CPI", "PPI", "NFP")

    def __init__(self, *, year_fn=None) -> None:
        # `year_fn` overridable for tests only; defaults to "this year" off
        # the real wall clock (see FomcSource's identical reasoning).
        self._year_fn = year_fn or (lambda: _dt.datetime.now(_dt.timezone.utc).year)

    async def fetch(self) -> list[CategoryFetch]:
        url = _url_for_year(self._year_fn())
        try:
            html = await fetch_text(url, allowed_host=_HOST)
        except Exception as exc:  # noqa: BLE001 -- CAL-09: never raise past fetch()
            return [CategoryFetch(category=c, ok=False, reason=f"fetch_failed:{exc!r}", url=url)
                    for c in self.categories]
        try:
            parsed = parse(html)
        except ValueError as exc:
            return [CategoryFetch(category=c, ok=False, reason=f"parse_failed:{exc}", url=url)
                    for c in self.categories]
        results: list[CategoryFetch] = []
        for category in self.categories:
            dates = parsed.get(category, ())
            if not dates:
                results.append(CategoryFetch(category=category, ok=False,
                                              reason="parse_empty", url=url))
            elif len(dates) > _MAX_PER_CATEGORY:
                results.append(CategoryFetch(category=category, ok=False,
                                              reason=f"implausible_count:{len(dates)}", url=url))
            else:
                results.append(CategoryFetch(category=category, ok=True, dates=dates, url=url))
        return results
