"""BeaSource -- CAL-09 tier-1 GDP/PCE fetch (bea.gov release schedule).

Fixture provenance: `tests/fixtures/calendar_sources/bea_schedule.html` is a
REAL, TRIMMED excerpt of the live page (captured 2026-07-16 via a plain
httpx GET, no header spoofing) -- only page chrome outside the schedule
table was removed; every table row this parser reads is real.

Real page structure: one `<table id="release-schedule-table">` whose
`<thead>` names the covered year ("Year 2026") and whose `<tbody>` rows
each carry a `<div class="release-date">Month Day</div>` and a
`release-title` cell naming the release. This ONE page covers BOTH tier-1
categories doc 11 assigns to bea.gov:
  * GDP -- every row whose title starts with "GDP" (the Advance/Second/
    Third estimate releases, each a real, separate decision-relevant date).
  * PCE -- BEA's official release name for the PCE price index is
    "Personal Income and Outlays" (rows starting with that exact title).

The page is a ROLLING near-term window (the captured sample ran from the
fetch date through the end of the named year), not a fixed annual
calendar -- IMPLEMENTATION DECISION, flagged: this parser trusts the single
`Year` header for every row it reads, which is wrong exactly at a
December/January rollover if the page ever shows a January row still under
a "Year <this year>" header (not observed in the captured sample; flagged
for operator awareness, not fixed here -- CAL-09's own vanished-date/
disputed machinery is what would surface such a date drifting a year off
in a SUBSEQUENT correct fetch, not this parser)."""
from __future__ import annotations

import re

from meic.adapters.calendar_sources.common import MONTHS, fetch_text
from meic.application.calendar_refresh import CategoryFetch

URL = "https://www.bea.gov/news/schedule"
_HOST = "www.bea.gov"

_YEAR_RE = re.compile(r'>Year (\d{4})<')
_ROW_RE = re.compile(r'<tr class="scheduled-releases-type-\w+">(.*?)</tr>', re.S)
_DATE_RE = re.compile(r'release-date">([^<]+)</div>')
_TITLE_RE = re.compile(r'release-title[^>]*>([^<]+)')

# No per-category band is named in CAL-09's ratified text for GDP/PCE (only
# FOMC's "6-10/yr" is given as an example) -- IMPLEMENTATION DECISION,
# flagged for operator ratification: GDP publishes 3 estimate vintages per
# quarter (~12/yr) and PCE is monthly (~12/yr); this generous upper-only
# cap exists purely to catch a corrupted/garbage parse (mirroring FOMC's
# own upper-bound-only reasoning), never to second-guess a legitimate count.
_MAX_PER_CATEGORY = 20


def parse(html: str) -> dict[str, tuple[str, ...]]:
    """Pure parse (independently tested against the real saved fixture) --
    {"GDP": (...), "PCE": (...)}, both tier-1 categories this ONE page
    covers. Raises ValueError if the year header or the table rows are not
    found at all -- the caller treats that as a rejected fetch."""
    m_year = _YEAR_RE.search(html)
    if not m_year:
        raise ValueError("no 'Year YYYY' table header found -- page structure unrecognised")
    year = int(m_year.group(1))
    rows = _ROW_RE.findall(html)
    if not rows:
        raise ValueError("no schedule table rows found -- page structure unrecognised")
    out: dict[str, set[str]] = {"GDP": set(), "PCE": set()}
    for row in rows:
        m_date, m_title = _DATE_RE.search(row), _TITLE_RE.search(row)
        if not m_date or not m_title:
            continue
        title = m_title.group(1).strip()
        parts = m_date.group(1).strip().split()
        if len(parts) != 2:
            continue
        month = MONTHS.get(parts[0])
        if month is None:
            continue
        try:
            day = int(parts[1])
            iso = f"{year:04d}-{month:02d}-{day:02d}"
            import datetime as _dt
            _dt.date(year, month, day)  # validate a REAL calendar date
        except ValueError:
            continue
        if title.startswith("GDP"):
            out["GDP"].add(iso)
        elif title.startswith("Personal Income and Outlays"):
            out["PCE"].add(iso)
    return {k: tuple(sorted(v)) for k, v in out.items()}


class BeaSource:
    categories = ("GDP", "PCE")

    async def fetch(self) -> list[CategoryFetch]:
        try:
            html = await fetch_text(URL, allowed_host=_HOST)
        except Exception as exc:  # noqa: BLE001 -- CAL-09: never raise past fetch()
            return [CategoryFetch(category=c, ok=False, reason=f"fetch_failed:{exc!r}", url=URL)
                    for c in self.categories]
        try:
            parsed = parse(html)
        except ValueError as exc:
            return [CategoryFetch(category=c, ok=False, reason=f"parse_failed:{exc}", url=URL)
                    for c in self.categories]
        results: list[CategoryFetch] = []
        for category in self.categories:
            dates = parsed.get(category, ())
            if not dates:
                results.append(CategoryFetch(category=category, ok=False,
                                              reason="parse_empty", url=URL))
            elif len(dates) > _MAX_PER_CATEGORY:
                results.append(CategoryFetch(category=category, ok=False,
                                              reason=f"implausible_count:{len(dates)}", url=URL))
            else:
                results.append(CategoryFetch(category=category, ok=True, dates=dates, url=URL))
        return results
