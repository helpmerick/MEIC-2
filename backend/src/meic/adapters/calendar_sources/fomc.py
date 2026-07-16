"""FomcSource -- CAL-09 tier-1 FOMC decision-day fetch (federalreserve.gov).

Fixture provenance: `tests/fixtures/calendar_sources/fomc_calendars_2026.html`
is a REAL, TRIMMED excerpt of the live page (captured 2026-07-16 via a plain
httpx GET, no header spoofing) -- only page chrome outside the meeting
panels was removed; every meeting-panel byte this parser reads is real.

Real page structure: year-scoped `<h4>YYYY FOMC Meetings</h4>` panel
headings, each followed by one `row fomc-meeting` div per meeting, carrying
a `fomc-meeting__month` and a `fomc-meeting__date`. The date field is one
of: a two-day range ("27-28"), an asterisked SEP-cycle range ("17-18*", the
asterisk marks a Summary of Economic Projections meeting -- irrelevant to
the decision date itself), a single day, or occasionally a non-meeting
"(notation vote)" entry (real, present in the fixture) which this parser
skips outright -- it carries no press conference / statement and is not a
tradeable-event decision day.

FOMC's decision day is convention: the LAST day of a (usually two-day)
meeting. A handful of real meetings cross a month boundary (e.g. "30-1",
also present in the fixture); `_decision_date` rolls the month/year forward
for those rather than guessing which month the lone trailing day number
belongs to.
"""
from __future__ import annotations

import datetime as _dt
import re

from meic.adapters.calendar_sources.common import MONTH_NAMES, fetch_text
from meic.application.calendar_refresh import CategoryFetch

URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_HOST = "www.federalreserve.gov"

_YEAR_HEADING_RE = re.compile(r'<h4><a id="\d+">(\d{4}) FOMC Meetings</a></h4>')
_ROW_START_RE = re.compile(r'<div class="(?:fomc-meeting--shaded )?row fomc-meeting"')
# The real page labels a meeting that crosses a month boundary with a
# COMBINED month string, e.g. "Jan/Feb" or "Apr/May" (verified against the
# real fixture -- a meeting like "30-1" is never under a plain "April", it
# is under "Apr/May") -- the "/" must be in the character class or the
# match fails outright and the whole row silently drops (the ORIGINAL,
# wrong version of this parser did exactly that, caught by
# tests/adapters/test_calendar_sources.py's month-crossing case against
# this same real fixture).
_MONTH_RE = re.compile(r'fomc-meeting__month[^>]*"><strong>([A-Za-z/]+)</strong>')
_DATE_RE = re.compile(r'fomc-meeting__date[^>]*">([^<]+)<')

# CAL-09 rule 1's own named example: "FOMC 6-10/yr". The lower bound is
# deliberately NOT enforced per-year here (see `_plausible` below) -- the
# most-recent forward-looking year legitimately has fewer than 6 meetings
# scheduled until the Fed publishes the rest of that year's calendar.
# IMPLEMENTATION DECISION, flagged for operator reversal (same C-flag
# culture as the rest of doc 11): the upper bound (10) is the one doing the
# real safety work, and is exactly what the ratified test's "40 FOMC dates"
# garbage-fetch scenario needs caught.
_MAX_PER_YEAR = 10


def _decision_date(year: int, month_name: str, date_str: str) -> str | None:
    if "notation vote" in date_str.lower():
        return None  # not a real meeting -- no press conference, no decision day
    cleaned = date_str.replace("*", "").strip()
    months = month_name.split("/")   # "April" (single) or "Jan/Feb" (crosses a boundary)
    if "-" in cleaned:
        start_s, _, end_s = cleaned.partition("-")
        try:
            start_day, end_day = int(start_s.strip()), int(end_s.strip())
        except ValueError:
            return None
        if len(months) == 2:
            # The decision day is the LAST day, under the SECOND named
            # month. December -> January is the only real year rollover.
            start_month, end_month = MONTH_NAMES.get(months[0]), MONTH_NAMES.get(months[1])
            if start_month is None or end_month is None:
                return None
            end_year = year + 1 if (start_month == 12 and end_month == 1) else year
            day = end_day
        else:
            month = MONTH_NAMES.get(months[0])
            if month is None:
                return None
            if end_day < start_day:
                # Defensive fallback only: the real site names a crossing
                # meeting with a combined "Mon1/Mon2" label (handled above),
                # so this should not trigger against a real fetch -- kept
                # as a safety net rather than silently mis-dating the day.
                end_month, end_year = (month + 1, year) if month < 12 else (1, year + 1)
            else:
                end_month, end_year = month, year
            day = end_day
    else:
        if len(months) != 1:
            return None
        end_month = MONTH_NAMES.get(months[0])
        if end_month is None:
            return None
        end_year = year
        try:
            day = int(cleaned)
        except ValueError:
            return None
    try:
        return _dt.date(end_year, end_month, day).isoformat()  # validates a REAL calendar date
    except ValueError:
        return None


def parse(html: str, *, min_year: int) -> tuple[str, ...]:
    """Pure parse (independently tested against the real saved fixture) --
    decision dates for `min_year` and later only; earlier years on the same
    page are dropped (irrelevant to a live blackout gate, and would skew
    the per-year plausibility band with data this refresh does not need).
    Raises ValueError if the page structure is not even recognisable (no
    year heading found at all) -- the caller treats that as a rejected fetch."""
    headers = [(m.start(), int(m.group(1))) for m in _YEAR_HEADING_RE.finditer(html)]
    if not headers:
        raise ValueError("no FOMC year headings found -- page structure unrecognised")
    row_starts = [m.start() for m in _ROW_START_RE.finditer(html)]
    dates: set[str] = set()
    for i, start in enumerate(row_starts):
        end = row_starts[i + 1] if i + 1 < len(row_starts) else len(html)
        block = html[start:end]
        year = None
        for h_pos, h_year in headers:
            if h_pos > start:
                break
            year = h_year
        if year is None or year < min_year:
            continue
        m_month, m_date = _MONTH_RE.search(block), _DATE_RE.search(block)
        if not m_month or not m_date:
            continue
        decision = _decision_date(year, m_month.group(1), m_date.group(1))
        if decision:
            dates.add(decision)
    return tuple(sorted(dates))


def _plausible(dates: tuple[str, ...]) -> bool:
    if not dates:
        return False
    per_year: dict[str, int] = {}
    for d in dates:
        per_year[d[:4]] = per_year.get(d[:4], 0) + 1
    return all(count <= _MAX_PER_YEAR for count in per_year.values())


class FomcSource:
    categories = ("FOMC",)

    def __init__(self, *, min_year_fn=None) -> None:
        # `min_year_fn` overridable for tests only; defaults to "this year"
        # off the real wall clock. A calendar-YEAR floor for the parser is
        # not a trading decision and does not need DAY-03's injected-clock
        # discipline the rest of this codebase enforces for trading logic.
        self._min_year_fn = min_year_fn or (lambda: _dt.datetime.now(_dt.timezone.utc).year)

    async def fetch(self) -> list[CategoryFetch]:
        try:
            html = await fetch_text(URL, allowed_host=_HOST)
        except Exception as exc:  # noqa: BLE001 -- CAL-09: never raise past fetch()
            return [CategoryFetch(category="FOMC", ok=False,
                                   reason=f"fetch_failed:{exc!r}", url=URL)]
        try:
            dates = parse(html, min_year=self._min_year_fn())
        except ValueError as exc:
            return [CategoryFetch(category="FOMC", ok=False,
                                   reason=f"parse_failed:{exc}", url=URL)]
        if not _plausible(dates):
            return [CategoryFetch(category="FOMC", ok=False,
                                   reason=f"implausible_count:{len(dates)}", url=URL)]
        return [CategoryFetch(category="FOMC", ok=True, dates=dates, url=URL)]
