"""adapters/calendar_sources.common -- CAL-09 (v1.77) shared read-only fetch
plumbing, doc 11.

Every source module in this package fetches ONE of the three named domains
ONLY (federalreserve.gov / bls.gov / bea.gov) -- structurally enforced HERE,
once, so no individual source can drift: `fetch_text` refuses to even send a
request whose URL host is not the exact allowed one, and refuses to FOLLOW a
redirect to a different host (a redirect is manually inspected, never
auto-followed by the HTTP client -- `follow_redirects=False`).

Read-only, unauthenticated GET only -- no write verb is ever used, no
credential is ever sent (CAL-09: "read-only, unauthenticated ... those named
domains ONLY"). A network/parse failure here must never raise past a
source's own `fetch()`: every source module converts an exception into a
`CategoryFetch(ok=False, reason=...)`, the same reject-don't-replace
discipline the rest of CAL-09 uses. This module itself only raises --
turning that into a safe result is each source's job (mirrors how
`meic.adapters.tastytrade.adapter` raises `NonCertTokenRefused`/
`NonProductionTokenRefused` before any network call and leaves the caller
to decide what a refusal means -- the house style for an external-I/O
boundary that must fail loudly to its own module and safely to its caller)."""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

import httpx

# A descriptive, honest User-Agent -- an identifying header, never an
# attempt to evade a site's bot-defense. See bls.py's module docstring for
# the real, observed finding that bls.gov's own WAF refuses ALL automated
# retrieval regardless of header content; this package does not attempt to
# work around that (CAL-09 mandates a read-only, unauthenticated -- never
# adversarial -- fetch).
USER_AGENT = ("MEIC-Bot-CalendarRefresh/1.0 "
              "(+https://github.com/astro8893/meic-bot-ddd-use-case-guide; "
              "read-only tier-1 calendar sync, CAL-09)")

MONTHS: dict[str, int] = {
    name: i for i, name in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}

# The federalreserve.gov page names a month-crossing meeting with a 3-letter
# ABBREVIATION pair (e.g. "Apr/May", "Jan/Feb", "Oct/Nov" -- verified against
# the real fixture), unlike its own full-name single-month meetings
# ("April", "March"). One combined lookup so a caller never has to guess
# which form it will see.
MONTH_NAMES: dict[str, int] = {**MONTHS, **{name[:3]: num for name, num in MONTHS.items()}}


class WrongHostRefused(RuntimeError):
    """A URL (or a redirect target) whose host is not the one this adapter
    is structurally pinned to, or whose scheme is not https -- refused
    before any request is sent for the original case, or before the
    redirect is followed for the second."""


class ResponseTooLarge(RuntimeError):
    """A response body over `MAX_RESPONSE_BYTES` -- refused before any
    parser sees it (review fix 3, 2026-07-16): a hijacked/garbage feed must
    not be able to feed megabytes into a regex parser or (via a future
    validation gap) the journal. Callers treat this like any other fetch
    failure: reject-don't-replace."""


# Review fix 3 (2026-07-16): the real captured pages are ~80-165 KB
# (fomc 164 KB, bea 82 KB); 5 MB is ~30x headroom over the largest while
# still refusing anything that could stall the health tick's parser or
# bloat memory. An internal safety bound in the SUBSCRIBE_SPAN_PTS style
# (an implementation constant, not an operator dial -- doc 06 defines no
# knob for it).
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


def _validate_url(url: str, *, allowed_host: str, context: str) -> None:
    """Host allowlist + https-only, applied identically to the original URL
    and to every redirect target (review fixes 5/6, 2026-07-16). A non-https
    scheme is refused even on the right host -- a downgrade redirect to
    http:// would silently strip transport integrity from an input that
    feeds NO-TRADE tags."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise WrongHostRefused(f"{context}: {url!r} is not https -- refused")
    if (parsed.hostname or "") != allowed_host:
        raise WrongHostRefused(f"{context}: {url!r} is not on the allowed host {allowed_host!r}")


async def fetch_text(url: str, *, allowed_host: str, timeout: float = 15.0,
                      transport: httpx.AsyncBaseTransport | None = None) -> str:
    """The ONE network call every source adapter in this package routes
    through. Raises on anything that is not a clean, size-bounded 200 from
    EXACTLY `allowed_host` over https -- the caller (each source's own
    `fetch()`) is responsible for turning that into a
    `CategoryFetch(ok=False, ...)`, never letting it escape as an unhandled
    exception (CAL-09: "a broken feed never blocks trading").

    Redirects (review fix 5, 2026-07-16): never auto-followed
    (`follow_redirects=False`). ONE manual hop is honoured -- the Location
    header is resolved against the request URL with `urljoin` (so a
    RELATIVE redirect resolves properly instead of silently re-requesting
    the original URL, the pre-fix bug), then re-validated against the SAME
    https+host rule as the original. A second redirect is refused outright.

    `transport` is test-only: an injected `httpx.MockTransport` lets
    tests/adapters/test_calendar_sources.py prove the host-allowlist,
    scheme, redirect and size-cap behaviour deterministically, with no real
    network call and no real server to stand up. Production callers never
    pass it (real network, real DNS/TLS)."""
    _validate_url(url, allowed_host=allowed_host, context="request")
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, transport=transport,
                                  headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(url)
        if resp.is_redirect:
            # Resolve relative Locations against the URL that was actually
            # requested (RFC 3986 via urljoin), THEN re-validate scheme+host
            # exactly like the original -- a redirect landing anywhere else
            # (another host, or an http:// downgrade) is refused, never
            # followed.
            location = resp.headers.get("location", "")
            next_url = urljoin(url, location)
            _validate_url(next_url, allowed_host=allowed_host, context="redirect")
            resp = await client.get(next_url)
            if resp.is_redirect:
                raise WrongHostRefused(
                    f"second redirect from {next_url!r} -- one manual hop only, refused")
    resp.raise_for_status()
    if len(resp.content) > MAX_RESPONSE_BYTES:
        raise ResponseTooLarge(
            f"{len(resp.content)} bytes from {url!r} exceeds {MAX_RESPONSE_BYTES} -- refused unparsed")
    return resp.text
