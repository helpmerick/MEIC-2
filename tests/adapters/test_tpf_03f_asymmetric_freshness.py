"""TPF-03f — asymmetric mark freshness on the EXIT path, conservative-only.

The spec/04 scenario, pinned line by line: a fresh short + a 20-second-old
long is EVALUABLE with the stale long taken CONSERVATIVELY and the use
RECORDED; a short staler than max_quote_age_ms makes the entry unevaluable;
a long older than exit_long_leg_max_age_ms makes the entry unevaluable.

WHY the asymmetry: a faster loop is only as live as its marks. The short
dominates cost-to-close, so it fails closed at max_quote_age_ms; a far-OTM
0DTE long routinely goes quiet and moves profit% very little, so it gets the
extended budget — but ALWAYS at the conservative value, so staleness can
never inflate computed profit. Error direction: fire EARLY, never late.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from meic.adapters.api.server import (
    _ExitFreshness,
    _exit_long_leg_max_age_ms,
    _resolve_exit_leg_mid,
)
from meic.domain.staleness import StampedQuote

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)
SHORT = "SPXW  260727P07435000"
LONG = "SPXW  260727P07385000"


class _Hub:
    def __init__(self, marks):
        self._marks = marks

    def mark(self, symbol):
        return self._marks.get(symbol)


class _Side:
    """A chain side whose _leg_mid resolves nothing — isolates the hub path."""


class _Snap:
    put_side = None
    call_side = None
    stale = False

    def __init__(self, streamer_map=None):
        self.streamer_symbols = streamer_map or {}


def _quote(sym, mid, age_ms):
    half = D("0.05")
    return StampedQuote(symbol=sym, bid=mid - half, ask=mid + half,
                        stamped_at=NOW - timedelta(milliseconds=age_ms))


def _snap_for(strike, put_streamer):
    return _Snap({strike: (put_streamer, ".C-unused")})


def test_tpf03f_dial_defaults_and_rejects():
    assert _exit_long_leg_max_age_ms({}) == 30000
    assert _exit_long_leg_max_age_ms({"MEIC_EXIT_LONG_LEG_MAX_AGE_MS": "2999"}) == 30000
    assert _exit_long_leg_max_age_ms({"MEIC_EXIT_LONG_LEG_MAX_AGE_MS": "120000"}) == 120000


def test_tpf03f_fresh_short_and_20s_old_long_is_evaluable_conservative_and_recorded():
    fresh = _ExitFreshness(long_max_age_ms=30000)
    hub = _Hub({".P-long": _quote(".P-long", D("0.50"), age_ms=20_000)})
    mid = _resolve_exit_leg_mid(LONG, "PUT", "long", _snap_for(D("7385"), ".P-long"),
                                D("7385"), hub=hub, now=NOW,
                                max_quote_age_ms=3000, freshness=fresh)
    # snapshot side is unmarked here, so the conservative min is the hub mark
    assert mid == D("0.50")
    assert fresh.stale_long_used, "TPF-03f: every stale-long use is RECORDED"
    assert fresh.reason is None


def test_tpf03f_a_stale_short_makes_the_entry_unevaluable_never_snapshot():
    """The short dominates cost-to-close: no silent fallback to a minute-old
    snapshot price — that fallback is exactly what made the 250 ms cadence
    partly illusory."""
    fresh = _ExitFreshness(long_max_age_ms=30000)
    hub = _Hub({".P-short": _quote(".P-short", D("3.80"), age_ms=10_000)})
    mid = _resolve_exit_leg_mid(SHORT, "PUT", "short", _snap_for(D("7435"), ".P-short"),
                                D("7435"), hub=hub, now=NOW,
                                max_quote_age_ms=3000, freshness=fresh)
    assert mid is None
    assert fresh.reason and "fail-closed" in fresh.reason


def test_tpf03f_a_long_over_the_extended_budget_is_unevaluable():
    fresh = _ExitFreshness(long_max_age_ms=30000)
    hub = _Hub({".P-long": _quote(".P-long", D("0.50"), age_ms=31_000)})
    mid = _resolve_exit_leg_mid(LONG, "PUT", "long", _snap_for(D("7385"), ".P-long"),
                                D("7385"), hub=hub, now=NOW,
                                max_quote_age_ms=3000, freshness=fresh)
    assert mid is None
    assert fresh.reason and "extended" in fresh.reason


def test_tpf03f_a_fresh_short_passes_straight_through():
    fresh = _ExitFreshness(long_max_age_ms=30000)
    hub = _Hub({".P-short": _quote(".P-short", D("3.80"), age_ms=500)})
    mid = _resolve_exit_leg_mid(SHORT, "PUT", "short", _snap_for(D("7435"), ".P-short"),
                                D("7435"), hub=hub, now=NOW,
                                max_quote_age_ms=3000, freshness=fresh)
    assert mid == D("3.80")
    assert fresh.reason is None and not fresh.stale_long_used


def test_tpf03f_staleness_never_inflates_profit():
    """The HARDENING clause: when both a stale hub mark and a snapshot mark
    exist for the long, the LOWER is taken — lower long mid means higher
    cost-to-close means LOWER profit, so the floor fires early, never late."""
    class _MarkedSide:
        pass

    from meic.adapters.api import server as srv

    fresh = _ExitFreshness(long_max_age_ms=30000)
    hub = _Hub({".P-long": _quote(".P-long", D("0.80"), age_ms=20_000)})
    snap = _snap_for(D("7385"), ".P-long")
    real_leg_mid = srv._leg_mid
    srv._leg_mid = lambda side, strike: D("0.40")     # snapshot says LOWER
    try:
        mid = _resolve_exit_leg_mid(LONG, "PUT", "long", snap, D("7385"),
                                    hub=hub, now=NOW, max_quote_age_ms=3000,
                                    freshness=fresh)
    finally:
        srv._leg_mid = real_leg_mid
    assert mid == D("0.40"), "the conservative (lower) long value must win"


def test_tpf03f_no_hub_at_all_keeps_the_snapshot_path():
    """Paper without a hub / pre-NFR-04 callers: the snapshot IS the mark
    source and DAT-02's stale gate governs — byte-identical to before."""
    from meic.adapters.api import server as srv

    fresh = _ExitFreshness(long_max_age_ms=30000)
    real_leg_mid = srv._leg_mid
    srv._leg_mid = lambda side, strike: D("1.23")
    try:
        mid = _resolve_exit_leg_mid(SHORT, "PUT", "short", _Snap(), D("7435"),
                                    hub=None, now=None, max_quote_age_ms=3000,
                                    freshness=fresh)
    finally:
        srv._leg_mid = real_leg_mid
    assert mid == D("1.23")
