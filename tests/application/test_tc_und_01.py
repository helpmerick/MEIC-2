"""TC-UND-01 (spec v1.86, UND-01/UND-02) — the RUT-phase underlying-profile
machinery: dollar math scales by the profile multiplier, RSK-04 sums a
mixed-underlying day at each entry's OWN multiplier (never a shared one),
and an unverified/unsupported underlying is REFUSED at config validation,
never guessed. /ES entries stay REFUSED this phase (UND-06 build order:
RUT first) — these tests exercise the domain-level multiplier machinery via
the /ES profile's ratified ×50 fact only; they never validate an /ES ENTRY
as accepted.

tests/features/TC-UND-01.feature (generated, read-only) is the definition of
done this binds to.
"""
from __future__ import annotations

import asyncio
from datetime import time
from decimal import Decimal as D

import pytest

from meic.domain.risk import day_worst_case, worst_case_loss
from meic.domain.schedule import EntrySpec, ScheduleDefaults, resolve, validate_entry
from meic.domain.underlying import PROFILES


def test_tc_und_01_dollar_math_uses_the_profile_multiplier():
    """UND-01/UND-02: identical width/credit/contracts price differently by
    the traded underlying's PROFILE multiplier — SPX ×100, /ES ×50 — via the
    PROFILES registry and `worst_case_loss(multiplier=...)`. Domain-level
    only: no /ES entry ever validates as accepted (that is UND-06's later
    /ES phase, not this one)."""
    width, credit, contracts = D("50"), D("4.00"), 2

    spx = PROFILES["SPX"]
    es = PROFILES["/ES"]
    assert spx.multiplier == D("100")
    assert es.multiplier == D("50")

    spx_wc = worst_case_loss(width, credit, contracts=contracts, multiplier=spx.multiplier)
    es_wc = worst_case_loss(width, credit, contracts=contracts, multiplier=es.multiplier)

    assert spx_wc == (width - credit) * D("100") * contracts == D("9200")
    assert es_wc == (width - credit) * D("50") * contracts == D("4600")
    assert es_wc == spx_wc / 2  # the SAME structural trade, half the dollars at /ES's multiplier


def test_tc_und_01_rsk04_sums_a_mixed_day_at_each_entrys_own_multiplier():
    """UND-02: day worst case = SPX-worst-case(×100) + ES-worst-case(×50),
    NEVER a shared multiplier — `day_worst_case` accepts an optional 4th
    per-entry `multiplier` element for exactly this case, while a 3-tuple
    entry (every pre-v1.86 caller) keeps using the day-wide default (100),
    byte-identical to before this rule existed (see
    tests/domain/test_schedule.py's own `test_day_worst_case_sums_per_entry_never_n_times_max`,
    which still passes unmodified)."""
    spx_entry = (D("50"), D("4.00"), 2, PROFILES["SPX"].multiplier)   # (50-4)*100*2 = 9200
    es_entry = (D("30"), D("2.00"), 1, PROFILES["/ES"].multiplier)    # (30-2)*50*1  = 1400

    total = day_worst_case([spx_entry, es_entry])
    assert total == D("9200") + D("1400") == D("10600")

    # The WRONG model: a single shared multiplier applied to both entries.
    wrong_shared_multiplier = day_worst_case([(D("50"), D("4.00"), 2), (D("30"), D("2.00"), 1)])
    assert wrong_shared_multiplier == D("12000")
    assert total != wrong_shared_multiplier


def test_tc_und_01_unverified_underlying_refused_never_guessed():
    """UND-01: a schedule row naming an unsupported ("XSP") underlying is
    REFUSED at validation, never guessed — "RUT" and "SPX" are accepted, and
    an UNSET row defaults to SPX, byte-identical to every pre-v1.86 schedule.

    UND-03/F3 (v1.86 /ES Stage 2, 2026-07-21): /ES graduated from Stage 1's
    blanket refusal ("UND-03 pending") to a force-close-gated profile -- an
    /ES row that doesn't override `eod_close_time` now resolves CLEANLY,
    using the PROFILE's own default (15:55 ET), so it no longer refuses here.
    The F3 refusal-without-a-valid-eod_close_time case is TC-UND-02's own
    coverage (tests/application/test_tc_und_02.py), not this test's."""
    defaults = ScheduleDefaults()

    def errors_for(name: str | None):
        spec = EntrySpec(time=time(10, 0), underlying=name)
        resolved = resolve(spec, defaults)
        return validate_entry(resolved, 0)

    xsp_errors = errors_for("XSP")
    assert any(e.field == "underlying" for e in xsp_errors)

    # /ES resolves cleanly via its own profile default (UND-03/F3) -- no
    # longer refused now that Stage 2 has landed the force-close invariant.
    assert errors_for("/ES") == []

    assert errors_for("RUT") == []
    assert errors_for("SPX") == []

    # UNSET defaults to SPX, byte-identical.
    unset_resolved = resolve(EntrySpec(time=time(10, 0)), defaults)
    assert unset_resolved.underlying == "SPX"
    assert validate_entry(unset_resolved, 0) == []


# --- integration seams (UND-02): the REAL production paths, not just the -------
# --- domain arithmetic ---------------------------------------------------------

def _condor(underlying=None, **over):
    """A fully-specified Condor through the REAL production dataclass
    (application/execute_entry.py) -- 50-wide wings, credit 4.00, 2 lots."""
    from meic.application.execute_entry import Condor

    base = dict(entry_number=1, put_short=D("6300"), call_short=D("6400"),
                put_short_mid=D("2.00"), call_short_mid=D("2.00"),
                mid_credit=D("4.00"), min_total_credit=D("2.00"),
                put_long=D("6250"), call_long=D("6450"), contracts=2)
    if underlying is not None:
        base["underlying"] = underlying
    return Condor(**{**base, **over})


def test_tc_und_01_execute_entry_worst_case_prices_at_the_condors_own_multiplier():
    """UND-02 through the REAL entry path: `ExecuteEntryAttempt.worst_case`
    resolves the CONDOR's own profile multiplier -- an /ES-profiled condor
    prices ×50, a RUT (and a default/unset) condor ×100. Domain-level only:
    no /ES ENTRY validation is involved (UND-06: /ES is a later phase; only
    its ratified ×50 multiplier fact is exercised)."""
    from meic.application.execute_entry import ExecuteEntryAttempt

    # (50 - 4.00) x multiplier x 2 contracts
    assert ExecuteEntryAttempt.worst_case(_condor("/ES")) == D("4600")    # ×50
    assert ExecuteEntryAttempt.worst_case(_condor("RUT")) == D("9200")    # ×100
    assert ExecuteEntryAttempt.worst_case(_condor("SPX")) == D("9200")    # ×100
    assert ExecuteEntryAttempt.worst_case(_condor()) == D("9200")         # unset -> SPX, byte-identical


def test_tc_und_01_reporting_resolves_each_entrys_journaled_multiplier():
    """UND-02 through the REAL reporting path: a `CondorFilled` journaled
    with underlying="RUT" folds to an EntryProjection carrying it, and
    `folds.entry_dollars` scales via `multiplier_of` (an "/ES"-projected
    entry proves the arithmetic bites: same per-share pnl, HALF the
    dollars). Also round-trips a non-default underlying through the event
    codec (to_dict/from_dict) -- replay-correct journaling."""
    from dataclasses import replace

    from meic.domain.events import CondorFilled, Event, FilledLeg
    from meic.domain.projection import entry_underlying, fold
    from meic.reporting.folds import entry_dollars, multiplier_of

    legs = (FilledLeg(symbol="RUTW  260721P02250000", right="P", role="short", qty=1),
            FilledLeg(symbol="RUTW  260721P02200000", right="P", role="long", qty=1),
            FilledLeg(symbol="RUTW  260721C02350000", right="C", role="short", qty=1),
            FilledLeg(symbol="RUTW  260721C02400000", right="C", role="long", qty=1))
    filled = CondorFilled(entry_id="2026-07-21#1", net_credit=D("4.00"),
                          legs=legs, underlying="RUT")

    # codec round-trip: the journaled underlying survives replay byte-exact
    revived = Event.from_dict(filled.to_dict())
    assert revived == filled and revived.underlying == "RUT"

    day = fold([filled])
    entry = day.entries["2026-07-21#1"]
    assert entry.underlying == "RUT"
    assert entry_underlying([filled], "2026-07-21#1") == "RUT"      # the FIX-2 fee seam
    assert entry_underlying([filled], "never-filled#9") == "SPX"    # absent -> default

    # RUT is x100 (same as SPX -- verified fact); the multiplier PATH is
    # proven by the /ES projection: same per-share pnl, half the dollars.
    assert multiplier_of(entry) == D("100")
    assert entry_dollars(entry) == D("4.00") * 100 * 1
    es_entry = replace(entry, underlying="/ES")
    assert multiplier_of(es_entry) == D("50")
    assert entry_dollars(es_entry) == D("4.00") * 50 * 1


def test_tc_und_01_config_validation_refuses_unverified_global_underlying():
    """UND-01 at the GLOBAL config key (doc 06 §11/§37): SPX and RUT are
    accepted, an unknown symbol ("XSP") and the not-yet-built "/ES" are
    ConfigRejected -- refused, never guessed; /ES's reason names the pending
    UND-03 phase."""
    from meic.config.validation import ConfigRejected, validate_config, validate_underlying

    validate_underlying("SPX")
    validate_underlying("RUT")
    validate_config({"underlying": "RUT"})   # the validate_config wiring, not just the helper
    validate_config({})                       # key absent -> defaults to SPX, nothing to refuse

    with pytest.raises(ConfigRejected) as xsp:
        validate_underlying("XSP")
    assert xsp.value.key == "underlying"
    assert xsp.value.reason == "unknown_or_unverified_underlying"

    with pytest.raises(ConfigRejected) as es:
        validate_config({"underlying": "/ES"})
    assert es.value.key == "underlying"
    assert "UND-03" in es.value.reason        # names the pending /ES phase


def test_tc_und_01_selector_refuses_a_mismatched_chain_snapshot():
    """UND-04 defense in depth (v1.86): a snapshot STAMPED underlying="SPX"
    reaching a SelectionConfig underlying="RUT" attempt is refused with skip
    reason `chain_snapshot_underlying_mismatch` -- never a Condor, even
    though the (mis)routed provider handed it over. And a router with NO
    stream for the row's underlying skips `no_chain_stream:RUT` -- never a
    fallback onto another underlying's chain."""
    from types import SimpleNamespace

    from meic.composition.live_selection import LiveCondorSelector, SelectionConfig
    from meic.domain.chain import ChainSide

    empty = ChainSide((), {})
    spx_snap = SimpleNamespace(stale=False, underlying="SPX",
                               put_side=empty, call_side=empty)

    async def provider():
        return spx_snap

    rut_config = SelectionConfig(underlying="RUT")

    # (a) the mismatch guard: routing handed over the WRONG underlying's chain
    selector = LiveCondorSelector(snapshot_provider=provider)
    from datetime import datetime, timezone

    when = datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc)
    condor, reason = asyncio.run(selector(when, 1, rut_config))
    assert condor is None
    assert reason == "chain_snapshot_underlying_mismatch"

    # (b) the router miss: no stream at all for RUT -> named skip, no fallback
    routed = LiveCondorSelector(snapshot_provider=provider,
                                snapshot_router=lambda name: None)
    condor, reason = asyncio.run(routed(when, 1, rut_config))
    assert condor is None
    assert reason == "no_chain_stream:RUT"

    # (c) control: the SAME stamped snapshot with a matching SPX row proceeds
    # past the guard (its next failure is a chain-shape failure, not the
    # mismatch guard -- proving the guard keys on the underlying alone).
    matching, match_reason = asyncio.run(
        LiveCondorSelector(snapshot_provider=provider)(when, 1, SelectionConfig()))
    assert match_reason != "chain_snapshot_underlying_mismatch"


# --- FIX-5 (UND-04, cert triage 2026-07-21): the index-spot fallback chain -----
# RUT's index dxfeed Quote publishes NaN bid/ask -- the tastytrade SDK's
# pydantic parser REJECTS such an event and skips it, so a Quote-only spot
# reader never yields and the snapshot times out spotless (the exact RUTW
# cert failure). `_index_spot` subscribes BOTH Quote and Trade, prefers the
# Quote MID when a two-sided quote arrives, else falls back to Trade last --
# and fails CLOSED (asyncio.TimeoutError) when neither yields, DAT-02
# unchanged. These drive it with a fake streamer -- the SDK's NaN-skip is
# modelled as "the Quote listener yields nothing", which is exactly what the
# parser's skip looks like from this side of the seam.

class _QuoteEvt:      # sentinel event classes -- no tastytrade import needed
    pass


class _TradeEvt:
    pass


class _FakeStreamer:
    """subscribe/listen-shaped like DXLinkStreamer; emits scripted events per
    event class, then ends the stream (a listener that ends without a usable
    event models the SDK skipping every NaN quote)."""

    def __init__(self, quotes=(), trades=()):
        self.subscribed: list[tuple[type, tuple[str, ...]]] = []
        self._events = {_QuoteEvt: tuple(quotes), _TradeEvt: tuple(trades)}

    async def subscribe(self, cls, symbols):
        self.subscribed.append((cls, tuple(symbols)))

    async def listen(self, cls):
        for event in self._events.get(cls, ()):
            yield event


def _spot(streamer, prefer):
    from meic.adapters.dxlink.chain_snapshot import _index_spot

    return asyncio.run(_index_spot(
        streamer, "RUT", quote_cls=_QuoteEvt, trade_cls=_TradeEvt,
        timeout_s=1.0, prefer=prefer, grace_s=0.05))


def test_tc_und_01_index_spot_falls_back_to_trade_last_when_quote_is_nan():
    """FIX-5: NaN-bid/ask index Quote (= the SDK skips it; the Quote stream
    yields nothing) + a valid Trade -> the spot resolves from the Trade's
    last price, stamped spot_source="trade_last" -- for BOTH hints (the hint
    orders preference, never disables the fallback)."""
    from types import SimpleNamespace

    trade = SimpleNamespace(event_symbol="RUT", price=2245.37)
    for prefer in ("trade", "quote"):
        spot, source = _spot(_FakeStreamer(trades=(trade,)), prefer)
        assert (spot, source) == (D("2245.37"), "trade_last")

    # Both Quote and Trade subscriptions went out (defense in depth: the
    # quote path stays live even under the "trade" hint).
    fake = _FakeStreamer(trades=(trade,))
    _spot(fake, "trade")
    assert {cls for cls, _ in fake.subscribed} == {_QuoteEvt, _TradeEvt}


def test_tc_und_01_index_spot_prefers_the_quote_mid_when_two_sided():
    """FIX-5: a two-sided index Quote wins (spot_source="quote_mid") -- the
    SPX-normal case, byte-equivalent to the old Quote-only mid."""
    from types import SimpleNamespace

    quote = SimpleNamespace(event_symbol="RUT", bid_price=2245.0, ask_price=2246.0)
    spot, source = _spot(_FakeStreamer(quotes=(quote,)), "quote")
    assert (spot, source) == (D("2245.5"), "quote_mid")


def test_tc_und_01_index_spot_fails_closed_when_neither_event_yields():
    """FIX-5: no usable Quote AND no usable Trade -> asyncio.TimeoutError,
    exactly the old Quote-only path's failure mode -- the snapshot fails
    closed (DAT-02), never a synthesized spot."""
    with pytest.raises(asyncio.TimeoutError):
        _spot(_FakeStreamer(), "trade")


def test_tc_und_01_profiles_carry_the_triaged_spot_event_hints():
    """FIX-5: SPX hints "quote" (its index Quote is two-sided -- the SPXW
    cert passes on quote mid); RUT hints "trade" (its index Quote is NaN
    bid/ask -- 2026-07-21 cert triage). The hint only orders preference."""
    assert PROFILES["SPX"].spot_event_hint == "quote"
    assert PROFILES["RUT"].spot_event_hint == "trade"


# --- FIX-5 grace branch (the one _index_spot path no prior test drove): a -------
# Trade arrives FIRST, then the two-sided Quote lands -- WITHIN the bounded
# grace window (returns quote_mid, the hint honoured) or NEVER within it
# (returns trade_last after the bounded grace, never the full timeout).

class _DelayedStreamer:
    """Trade yields immediately; the Quote (if any) yields after `quote_delay`
    seconds -- so the Trade deterministically wins the first race and the
    grace branch is exercised. A `None` quote models "no two-sided Quote ever"
    (the listener ends yielding nothing)."""

    def __init__(self, *, trade, quote=None, quote_delay=0.0):
        self._trade, self._quote, self._quote_delay = trade, quote, quote_delay
        self.subscribed: list = []

    async def subscribe(self, cls, symbols):
        self.subscribed.append((cls, tuple(symbols)))

    async def listen(self, cls):
        if cls is _TradeEvt:
            if self._trade is not None:
                yield self._trade
            return
        if cls is _QuoteEvt:
            if self._quote is not None:
                await asyncio.sleep(self._quote_delay)
                yield self._quote
            return


def test_tc_und_01_index_spot_grace_lets_a_late_quote_win_under_quote_hint():
    """FIX-5 grace branch: prefer="quote", a Trade lands first but a two-sided
    Quote arrives WITHIN the grace window -> the Quote mid wins
    (spot_source="quote_mid"). The grace exists precisely so an index that
    normally quotes isn't pinned to a racing trade print."""
    from types import SimpleNamespace
    from meic.adapters.dxlink.chain_snapshot import _index_spot

    trade = SimpleNamespace(event_symbol="RUT", price=6001.0)
    quote = SimpleNamespace(event_symbol="RUT", bid_price=6000.0, ask_price=6002.0)
    streamer = _DelayedStreamer(trade=trade, quote=quote, quote_delay=0.02)

    spot, source = asyncio.run(_index_spot(
        streamer, "RUT", quote_cls=_QuoteEvt, trade_cls=_TradeEvt,
        timeout_s=1.0, prefer="quote", grace_s=0.30))   # grace >> quote_delay
    assert (spot, source) == (D("6001"), "quote_mid")


def test_tc_und_01_index_spot_grace_expires_to_trade_last_under_quote_hint():
    """FIX-5 grace branch: prefer="quote", a Trade lands first and the Quote
    does NOT arrive within the BOUNDED grace -> settle for trade_last (never
    the full timeout: a trade-only index must not be starved)."""
    from types import SimpleNamespace
    from meic.adapters.dxlink.chain_snapshot import _index_spot

    trade = SimpleNamespace(event_symbol="RUT", price=2300.0)
    quote = SimpleNamespace(event_symbol="RUT", bid_price=2299.0, ask_price=2301.0)
    # quote_delay far exceeds the grace, so the grace expires trade-only.
    streamer = _DelayedStreamer(trade=trade, quote=quote, quote_delay=1.0)

    spot, source = asyncio.run(_index_spot(
        streamer, "RUT", quote_cls=_QuoteEvt, trade_cls=_TradeEvt,
        timeout_s=2.0, prefer="quote", grace_s=0.05))    # grace << quote_delay
    assert (spot, source) == (D("2300"), "trade_last")


# --- FIX-6 (BLOCKING): the manual ENT-09 floor guard routes to THE ROW's own ---
# underlying's spot -- a RUT fire's floors must be evaluated against a RUT
# spot, never SPX's. Left zero-arg (the bug), floor_inside_spot compares
# RUT-scale floors (~2300) against an SPX spot (~6300): every RUT call floor
# spuriously refused, and -- the dangerous half -- every RUT put floor fails
# OPEN, disarming the ENT-09b race guard.

def test_tc_und_01_manual_fire_floor_guard_uses_the_rows_own_underlying_spot():
    from types import SimpleNamespace

    from meic.application.manual_entry import ManualEntry
    from meic.domain.walk import floor_inside_spot

    # The live server provider's per-underlying shape: SPX ~6300, RUT ~2300.
    spots = {"SPX": D("6300"), "RUT": D("2300")}
    routed = ManualEntry(SimpleNamespace(), None, None,
                         spot_provider=lambda underlying="SPX": spots.get(underlying))

    rut_row = SimpleNamespace(underlying="RUT")
    spx_row = SimpleNamespace(underlying="SPX")

    # FIX-6: the guard resolves THIS ROW's own underlying's spot (routed).
    assert routed._row_spot(rut_row) == D("2300")
    assert routed._row_spot(spx_row) == D("6300")

    # The dangerous half proven: a RUT put floor ABOVE the RUT spot has
    # crossed the money and MUST refuse. Against the correct (routed) RUT
    # spot it refuses; against the SPX spot the guard would fail OPEN.
    put_floor = D("2350")
    assert floor_inside_spot(routed._row_spot(rut_row),
                             put_floor=put_floor, call_floor=None) is True   # refused (correct)
    assert floor_inside_spot(spots["SPX"],
                             put_floor=put_floor, call_floor=None) is False  # the disarmed bug

    # A legacy ZERO-ARG provider (pre-v1.86 fakes) is tolerated -- the
    # TypeError fallback retries with no argument.
    legacy = ManualEntry(SimpleNamespace(), None, None, spot_provider=lambda: D("6300"))
    assert legacy._row_spot(rut_row) == D("6300")

    # No provider wired -> spot unknowable -> None (the check is skipped, an
    # honest absence, never a refusal on a guess).
    assert ManualEntry(SimpleNamespace(), None, None,
                       spot_provider=None)._row_spot(rut_row) is None


# --- FIX-10 (v1.86, 2026-07-21): ad-hoc (ENT-11) fire of a non-armed, ----------
# non-open underlying. `_Snapshots._wanted()` = schedule rows ∪ open entries,
# so an ad-hoc RUT fire (transient row, not persisted, no open RUT entry) has
# no stream and selection would skip `no_chain_stream:RUT` -- while ad-hoc SPX
# rides the {SPX} fallback. `_Snapshots.ensure(underlying)` provisions the
# stream just-in-time (the ad-hoc analog of the scheduled ENT-08 warm-up) so
# ad-hoc RUT fires like ad-hoc SPX; fail-closed if the chain can't be
# provisioned. These drive the REAL live-app registry with `snapshot_chain`
# faked per option-root -- no live connection.

def _jwt(iss: str) -> str:
    import base64
    import json

    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


def _und10_cert_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TT_CERT_PROVIDER_SECRET", "s")
    monkeypatch.setenv("TT_CERT_REFRESH_TOKEN", _jwt("https://api.sandbox.tastyworks.com"))
    monkeypatch.setenv("TT_CERT_ACCOUNT", "5WZ00000")
    monkeypatch.setenv("MEIC_LIVE_IS_TEST", "true")
    monkeypatch.setenv("MEIC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEIC_USER_PASSWORD", "panel-secret")


def _und10_app(monkeypatch, tmp_path, *, chain_by_root):
    from meic.adapters.api.server import live_app

    _und10_cert_env(monkeypatch, tmp_path)
    app = live_app()

    async def _fake_snapshot_chain(session, *, underlying="SPXW", index_symbol="SPX",
                                   now=None, **kw):
        return chain_by_root(underlying)

    import meic.adapters.dxlink.chain_snapshot as _cs_mod
    monkeypatch.setattr(_cs_mod, "snapshot_chain", _fake_snapshot_chain)
    return app


def _und10_healthy_side(direction, n: int = 25):
    """A realistic chain side with >= min_validated_strikes(10) reachable,
    two-sided-marked strikes on the default SelectionConfig() -- enough for a
    real selection (mirrors test_live_app.py's own `_healthy_side`)."""
    from meic.domain.chain import ChainSide, Mark

    spot = D("6000")
    strikes = tuple(spot + direction * D(5 * i) for i in range(n))
    marks = {}
    for i, s in enumerate(strikes):
        mid = max(D("0.15"), D("3.60") - D("0.30") * i)
        marks[s] = Mark(bid=mid - D("0.05"), ask=mid + D("0.05"))
    return ChainSide(strikes, marks)


def _und10_rut_snap(root):
    """A healthy chain snapshot stamped for its own underlying (RUTW->RUT,
    SPXW->SPX) -- the source stamp the UND-04 mismatch guard checks."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    return SimpleNamespace(
        spot=D("6000"), stale=False, taken_at=datetime(2026, 7, 21, 15, 30, tzinfo=timezone.utc),
        underlying="RUT" if root == "RUTW" else "SPX",
        put_side=_und10_healthy_side(D(-1)), call_side=_und10_healthy_side(D(1)))


def test_tc_und_01_adhoc_ensure_provisions_a_rut_stream_so_selection_reads_the_rut_chain(
        monkeypatch, tmp_path):
    """FIX-10 (a): with NO armed RUT row and no open RUT entry, an ad-hoc RUT
    selection first skips `no_chain_stream:RUT`; after `ensure("RUT")`
    provisions the stream, selection reads the RUT chain and returns a REAL
    Condor stamped underlying="RUT" -- never a no_chain_stream skip."""
    from datetime import datetime, timezone

    from meic.composition.live_selection import SelectionConfig

    app = _und10_app(monkeypatch, tmp_path, chain_by_root=_und10_rut_snap)
    comp = app.state.composition
    snaps = app.state.chain_snapshots
    selector = app.state.runtime.selector
    when = datetime(2026, 7, 21, 15, 30, tzinfo=timezone.utc)

    comp.state.entry_schedule = []          # nothing armed
    assert snaps.snapshot_for("RUT") is None  # RUT not wanted -> no stream built

    # Without ensure(): the ad-hoc RUT selection fail-closes (the pre-FIX-10
    # un-firable behaviour).
    condor, reason = asyncio.run(selector(when, 101, SelectionConfig(underlying="RUT")))
    assert condor is None and reason == "no_chain_stream:RUT"

    # ensure() provisions the stream just-in-time (idempotent, on demand).
    asyncio.run(snaps.ensure("RUT"))
    assert snaps.snapshot_for("RUT") is not None

    # Now selection reads the RUT chain -> a real Condor, underlying RUT.
    condor, reason = asyncio.run(selector(when, 102, SelectionConfig(underlying="RUT")))
    assert reason is None and condor is not None
    assert condor.underlying == "RUT"


def test_tc_und_01_adhoc_ensure_fails_closed_when_the_chain_cannot_be_provisioned(
        monkeypatch, tmp_path):
    """FIX-10 (b): `ensure()` when `snapshot_chain` raises -> the transient
    stream is dropped, a warning names the underlying, NO exception escapes
    ensure(), and the subsequent ad-hoc selection fail-closes to the named
    `no_chain_stream:RUT` skip (never an exception into the fire path)."""
    from datetime import datetime, timezone

    from meic.composition.live_selection import SelectionConfig

    def chain(root):
        if root == "RUTW":
            raise RuntimeError("transient RUTW provision failure")
        return _und10_rut_snap(root)

    app = _und10_app(monkeypatch, tmp_path, chain_by_root=chain)
    comp = app.state.composition
    snaps = app.state.chain_snapshots
    selector = app.state.runtime.selector
    when = datetime(2026, 7, 21, 15, 30, tzinfo=timezone.utc)
    comp.state.entry_schedule = []

    # ensure() swallows the provision failure (no exception), warns naming RUT,
    # and leaves NO re-raising stream behind.
    asyncio.run(snaps.ensure("RUT"))
    assert "RUT" not in snaps._streams
    assert any(a["level"] == "warning" and "RUT" in a["message"]
               for a in comp.alerts.recent())

    # The fire's selection therefore fail-closes to a NAMED skip, not a raise.
    condor, reason = asyncio.run(selector(when, 101, SelectionConfig(underlying="RUT")))
    assert condor is None and reason == "no_chain_stream:RUT"


def test_tc_und_01_adhoc_ensured_stream_is_pruned_after_pin_lapses_but_survives_if_open(
        monkeypatch, tmp_path):
    """FIX-10 (c) + FIX-11: `ensure()` pins the ad-hoc underlying only
    TRANSIENTLY (never permanently wanted). While the pin is LIVE the stream
    survives sync() (FIX-11: it must not be pruned mid-fire); once the pin
    LAPSES an unfilled ad-hoc stream is PRUNED (FIX-10's cleanup, deferred
    past the fire window); a FILLED entry survives regardless of the pin via
    the open-entry set. A deterministic monotonic clock (`snaps._now`) is
    advanced -- no real sleeps."""
    from meic.domain.events import CondorFilled, FilledLeg

    app = _und10_app(monkeypatch, tmp_path, chain_by_root=_und10_rut_snap)
    comp = app.state.composition
    snaps = app.state.chain_snapshots

    clock = {"t": 1000.0}
    snaps._now = lambda: clock["t"]         # FIX-11: inject the monotonic source
    ttl = snaps._pin_ttl_s

    comp.state.entry_schedule = []          # no armed RUT row
    asyncio.run(snaps.ensure("RUT"))
    assert "RUT" in snaps._streams          # provisioned + pinned
    assert "RUT" in snaps._wanted()         # FIX-11: the LIVE pin unions into wanted

    # While the pin is still live, a sync() must NOT prune it (the FIX-11
    # guarantee -- a concurrent health tick can't strand an in-flight fire).
    clock["t"] += ttl / 2
    snaps.sync()
    assert "RUT" in snaps._streams

    # Once the pin LAPSES, an unfilled ad-hoc stream is pruned (FIX-10 cleanup,
    # just deferred past the fire window).
    clock["t"] += ttl                       # now well past the pin expiry
    assert "RUT" not in snaps._wanted()
    snaps.sync()
    assert "RUT" not in snaps._streams
    assert "RUT" not in snaps._pinned        # the lapsed pin itself is dropped

    # Re-provision, then the ad-hoc entry FILLS (an open RUT entry now exists):
    # it is wanted via the OPEN-ENTRY set and survives even after the pin
    # lapses -- an open position never loses its stream.
    asyncio.run(snaps.ensure("RUT"))
    comp.events.append(CondorFilled(
        entry_id="2026-07-21#101", net_credit=D("4.00"), underlying="RUT",
        legs=(FilledLeg(symbol="RUTW  260721P05950000", right="P", role="short", qty=1),)))
    clock["t"] += 2 * ttl                   # pin long expired
    assert "RUT" not in snaps._pinned or snaps._pinned["RUT"] <= snaps._now()
    assert "RUT" in snaps._wanted()          # open-entry set, not the pin
    snaps.sync()
    assert "RUT" in snaps._streams           # survives -- open position keeps its stream


def test_tc_und_01_fix11_pin_keeps_an_in_flight_stream_alive_across_a_concurrent_sync(
        monkeypatch, tmp_path):
    """FIX-11 (the race): `ensure("RUT")` sets the pin BEFORE the (1-20s
    network) prime, so a concurrent ~60s health-tick `sync()` running WHILE
    the prime is in flight -- with wanted computing {SPX} (no armed RUT row,
    no open RUT entry) -- must NOT prune the RUT stream. Driven
    deterministically with a two-event handshake (no real sleeps): the gated
    `snapshot_chain` signals `entered` once it is blocked mid-prime, the test
    runs sync() at exactly that point, then releases `gate`."""
    from datetime import datetime, timezone

    from meic.adapters.api.server import live_app

    _und10_cert_env(monkeypatch, tmp_path)
    app = live_app()
    comp = app.state.composition
    snaps = app.state.chain_snapshots
    comp.state.entry_schedule = []          # RUT is NOT wanted (no row, no open entry)

    entered = asyncio.Event()               # set once the prime is blocking
    gate = asyncio.Event()                  # released to let the prime finish

    async def _gated_snapshot_chain(session, *, underlying="SPXW", index_symbol="SPX",
                                    now=None, **kw):
        if underlying == "RUTW":
            entered.set()
            await gate.wait()               # hold the RUT prime open, mid-flight
        return _und10_rut_snap(underlying)

    import meic.adapters.dxlink.chain_snapshot as _cs_mod
    monkeypatch.setattr(_cs_mod, "snapshot_chain", _gated_snapshot_chain)

    async def _drive():
        ensure_task = asyncio.ensure_future(snaps.ensure("RUT"))
        await entered.wait()                # ensure() has pinned + created the stream and is
                                            # now awaiting inside the gated prime
        # A concurrent health-tick sync() lands RIGHT HERE, mid-prime. Without
        # the FIX-11 pin it would compute wanted={SPX} and delete the RUT
        # stream; the pin unions RUT in, so it SURVIVES.
        snaps.sync()
        assert "RUT" in snaps._streams, "the pin must keep the in-flight stream alive"
        assert snaps.provider_for("RUT") is not None
        assert "RUT" in snaps._live_pins(snaps._now())

        gate.set()                          # release the prime
        await ensure_task
        assert snaps.snapshot_for("RUT") is not None   # primed and readable

    asyncio.run(_drive())


# --- UND-05 (v1.86 loosening, operator ruling 2026-07-21): the OUTER ENT-03 ----
# data_fresh gate now resolves PER THE ATTEMPT'S OWN underlying, never the
# aggregate `any(stream.stale for stream in streams)`. The selector's own
# `_attempt` (live_selection.py) already fail-closes per-underlying (a routed
# snapshot that is None/stale skips `data_unavailable`/`no_chain_stream`); this
# closes the LAST aggregate surface -- the outer gate that used to block
# EVERY entry (including a healthy SPX) whenever ANY wanted stream (e.g. a
# RUT-only outage) went stale. DAT-02 stays fail-closed throughout: an
# absent/unbuilt stream reads NOT fresh, never guessed fresh.

def test_tc_und_05_missing_or_unbuilt_stream_reads_not_fresh(monkeypatch, tmp_path):
    """UND-05/DAT-02: an underlying with no armed row, no open entry, and no
    `ensure()` provisioning has NO stream at all -- `fresh_for` must read
    False (fail-closed), never True for an absent stream."""
    app = _und10_app(monkeypatch, tmp_path, chain_by_root=_und10_rut_snap)
    comp = app.state.composition
    snaps = app.state.chain_snapshots

    comp.state.entry_schedule = []          # nothing armed -> {SPX} legacy fallback only
    asyncio.run(snaps.take())               # builds/refreshes only the SPX stream

    assert snaps.snapshot_for("RUT") is None            # never built
    assert snaps.fresh_for("RUT") is False              # fail-closed, not a guess
    assert snaps.fresh_for("SPX") is True                # SPX WAS built and is fresh


def test_tc_und_05_stale_rut_stream_does_not_block_a_fresh_spx_entry(monkeypatch, tmp_path):
    """UND-05 -- THE ruling's exact scenario: SPX fresh, RUT stale. The
    real, live-wired `data_fresh`/`LiveMarketGates` gate reads True for an
    SPX attempt (fires) and False for a RUT attempt (blocked) off the SAME
    provider -- a RUT-only data outage must never take SPX down with it."""
    app = _und10_app(monkeypatch, tmp_path, chain_by_root=_und10_rut_snap)
    comp = app.state.composition
    snaps = app.state.chain_snapshots
    gates = app.state.runtime.market_gates   # the REAL LiveMarketGates instance

    # Arm both underlyings so the probe cadence builds BOTH streams.
    comp.state.entry_schedule = [{"time": "10:00", "underlying": "SPX"},
                                 {"time": "10:05", "underlying": "RUT"}]
    asyncio.run(snaps.take())

    assert snaps.snapshot_for("SPX") is not None and snaps.snapshot_for("RUT") is not None
    assert snaps.fresh_for("SPX") is True and snaps.fresh_for("RUT") is True

    # A transient RUTW-only outage: mark ONLY the RUT stream stale.
    snaps._streams["RUT"].stale = True

    assert snaps.fresh_for("SPX") is True     # unaffected by RUT's outage
    assert snaps.fresh_for("RUT") is False    # fail-closed for RUT alone

    spx_gate = asyncio.run(gates("SPX"))
    rut_gate = asyncio.run(gates("RUT"))
    assert spx_gate.data_fresh is True         # SPX entries still fire
    assert rut_gate.data_fresh is False        # RUT entries blocked
    # every OTHER gate input reads the SAME (real broker/session probe) way
    # regardless of which underlying was named -- only data_fresh varies.
    assert spx_gate.session_valid == rut_gate.session_valid
    assert spx_gate.buying_power_ok == rut_gate.buying_power_ok


def test_tc_und_05_a_single_underlying_spx_run_gates_byte_identically_to_before(
        monkeypatch, tmp_path):
    """UND-05: the aggregate/paper/single-underlying path is UNCHANGED. With
    only SPX ever wanted (the legacy `{"SPX"}` fallback, no RUT anywhere),
    `snaps.stale` (the pre-v1.86 aggregate DAT-02 gate every other consumer
    still reads) and `snaps.fresh_for("SPX")`/the bare `await gates()` call
    (default underlying) always agree -- there is only ever one stream to
    disagree about."""
    app = _und10_app(monkeypatch, tmp_path, chain_by_root=_und10_rut_snap)
    comp = app.state.composition
    snaps = app.state.chain_snapshots
    gates = app.state.runtime.market_gates

    comp.state.entry_schedule = []          # nothing armed -> {SPX} only
    asyncio.run(snaps.take())

    assert snaps.stale is False
    assert snaps.fresh_for("SPX") is True
    assert asyncio.run(gates()).data_fresh is True    # bare call -> SPX default

    snaps.stale = True                       # the legacy aggregate setter (writes SPX)
    assert snaps.fresh_for("SPX") is False
    assert asyncio.run(gates()).data_fresh is False   # bare call still tracks it


def test_tc_und_05_live_market_gates_defaults_to_spx_and_tolerates_legacy_zero_arg_data_fresh():
    """UND-05 at the `LiveMarketGates` unit level (no live_app needed): a
    bare `await gates()` resolves "SPX", and a LEGACY zero-arg `data_fresh`
    provider (pre-v1.86 paper/tests, no `underlying` parameter at all) is
    tolerated via the `TypeError` fallback -- the identical dual-arity idiom
    `manual_entry._row_spot`'s own `spot_provider` fallback already uses.
    Every OTHER gate input (session_valid/buying_power_ok/flatten_in_progress)
    is untouched -- only `data_fresh` is ever offered the underlying."""
    from datetime import datetime, timezone

    from meic.composition.live_gates import LiveMarketGates

    async def legacy_data_fresh():           # zero-arg: no `underlying` param at all
        return True

    async def ok():
        return True

    class _Clock:
        def now(self):
            return datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc)

    gates = LiveMarketGates(clock=_Clock(), data_fresh=legacy_data_fresh,
                            session_valid=ok, buying_power_ok=ok)

    bare_snap = asyncio.run(gates())          # no args at all -- pre-v1.86 call shape
    assert bare_snap.data_fresh is True

    named_snap = asyncio.run(gates("RUT"))    # a real caller naming RUT still works
    assert named_snap.data_fresh is True       # legacy provider ignores the name, always True


def test_tc_und_05_nfr07_data_fresh_live_check_still_proves_stale_to_false(
        monkeypatch, tmp_path):
    """NFR-07 (composition/wiring_registry.py's `_data_fresh_live_check`)
    must still prove the bound `data_fresh` gate input is a REAL,
    live-flippable signal, not a constant -- UND-05 changed data_fresh's
    SHAPE (it now takes an optional underlying) but not this guarantee: the
    live-check's bare `provider()` call resolves the "SPX" default, and
    `snaps.stale`'s setter writes through to that same default stream."""
    from meic.composition.wiring_registry import SAFETY_GATE_REGISTRY

    app = _und10_app(monkeypatch, tmp_path, chain_by_root=_und10_rut_snap)
    comp = app.state.composition
    comp.state.entry_schedule = []          # nothing armed -> {SPX} only, matches pre-v1.86

    entry = next(e for e in SAFETY_GATE_REGISTRY if e.gate_input == "data_fresh")
    assert "DAT-02" in entry.rule_ids and "ENT-03" in entry.rule_ids

    result = entry.live_check(app.state)
    assert result.live is True, result.detail
