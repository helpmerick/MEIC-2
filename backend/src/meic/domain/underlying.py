"""Underlying profiles — UND-01/02/03/04/05/06 (v1.86, operator-ratified:
SPX + RUT + /ES).

The traded underlying is a first-class per-schedule-row parameter (UND-01).
Each supported underlying is a ratified CODE-CONSTANT profile — known facts,
never a fetched dial — the same pattern DAY-01a's NYSE calendar already
uses: the amendment fixes the SHAPE (what a profile carries), the broker/
exchange specs fix the NUMBERS, and every fact is VERIFIED before building
("the agent MUST VERIFY every profile fact against current broker/exchange
specs", UND-01). An unknown or unverified underlying is REFUSED at config
validation, never guessed (see `profile_for` below, and
`domain/schedule.py::validate_entry` / `config/validation.py::validate_underlying`,
the two callers that turn a `None`/disabled profile into a refusal).

Pure domain: no I/O (CLAUDE.md rule 6). STK-08's "tick rules come from the
broker API, never hardcoded" governs actual ORDER PLACEMENT; the facts here
are the ratified SHAPE the strategy runs against, same class as the trading
calendar's algorithmic holiday rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal

from .ticks import TickRung, TickTable

# SPX and RUT share an IDENTICAL tick structure — both cash-settled Cboe index
# options: $0.05 below $3.00, $0.10 at-or-above (verified, see each profile's
# own citation below). UND-02 FLAG: this shared constant is correct for BOTH
# profiles this phase; the /ES phase must make ticks genuinely per-profile
# once futures-option tick rules are verified (see adapters/api/server.py's
# own `SPX` TickTable comment, which flags the same fact for the live wiring
# constant it builds from real broker data).
_INDEX_TICKS = (TickRung(Decimal("3.00"), Decimal("0.05")), TickRung(None, Decimal("0.10")))


@dataclass(frozen=True)
class UnderlyingProfile:
    """One ratified underlying (UND-01). Every fact is comment-cited to its
    verified source at the point it is set below — never re-derived, never
    guessed. `enabled=False` (this phase: only `/ES`) means the profile is a
    KNOWN name but not yet tradable — `profile_for` still resolves it (a
    pure lookup), so callers that need to refuse a disabled underlying check
    `.enabled` themselves and report `.disabled_reason`."""

    name: str                       # "SPX" | "RUT" | "/ES"
    index_symbol: str               # the underlying INDEX/future symbol dxlink subscribes to
    option_root: str                # OCC option root (e.g. "SPXW") — UND-04 symbology
    instrument_class: str           # "cash_index" | "futures_option"
    multiplier: Decimal             # UND-02: the dollar multiplier every money calc scales by
    settlement: str                 # "cash" | "physical_futures_assignment"
    expiring_last_trade: time       # ET wall-clock, last trading time of an EXPIRING series
    session_open: time              # UND-05: the RTH session this bot trades (unchanged from SPX)
    session_close: time
    tick_rungs: tuple[TickRung, ...]
    strike_increment_note: str      # human-readable — the near-the-money strike step
    daily_0dte: bool                # True: genuine Mon-Fri daily 0DTE expiries exist
    margin_model: str
    # UND-02 (v1.86): "Per-underlying credit floors, wing-width/probe/
    # chain-completeness defaults ... are set per profile (SPX values
    # unchanged)." These are PREFILL/DEFAULT dials only — a schedule row
    # pins its own concrete values at Save (v1.47, never retro-applied), and
    # the env dials (`chain_completeness_pct`/`min_validated_strikes`), when
    # SET by the operator, override the chain-gate defaults for ALL
    # underlyings (precedence documented at
    # composition/live_selection.py::LiveCondorSelector).
    default_target_premium: Decimal = Decimal("3.00")
    default_wing_width: Decimal = Decimal("50")
    default_min_short_premium: Decimal = Decimal("1.00")
    default_min_total_credit: Decimal = Decimal("2.00")
    default_probe_down_max: int = 25
    default_chain_completeness_pct: Decimal = Decimal("90")
    default_min_validated_strikes: int = 10
    # UND-04 (v1.86, FIX-5 -- cert triage 2026-07-21): which dxfeed event the
    # INDEX's spot normally comes from -- "quote" (two-sided index Quote,
    # SPX) or "trade" (Trade last price; RUT's index Quote publishes NaN
    # bid/ask). Used ONLY to ORDER the subscription preference in
    # `adapters/dxlink/chain_snapshot._index_spot`; BOTH event paths stay
    # live for BOTH profiles (defense in depth, same philosophy as the
    # selector's snapshot-underlying mismatch guard), and neither yielding a
    # number still fails the snapshot closed (DAT-02 unchanged).
    spot_event_hint: str = "quote"
    enabled: bool = True
    disabled_reason: str | None = None

    @property
    def ticks(self) -> TickTable:
        """A ready TickTable built from this profile's rungs. UND-02 FLAG:
        production order placement does not yet consult this (see
        adapters/api/server.py's `SPX` TickTable module comment) — this
        phase's tick wiring stays exactly as it was, deliberately (see
        implementation notes for UND-01..06, "Do NOT restructure tick wiring
        this phase")."""
        return TickTable(self.tick_rungs)


PROFILES: dict[str, UnderlyingProfile] = {
    "SPX": UnderlyingProfile(
        name="SPX",
        index_symbol="SPX",
        option_root="SPXW",
        instrument_class="cash_index",
        multiplier=Decimal("100"),
        settlement="cash",
        # SPX: x100/pt; cash-settled European; SPXW root = PM-settled with TRUE
        # Mon-Fri daily 0DTE expiries; expiring SPXW series last trade 16:00 ET
        # (non-expiring 16:15 ET); ticks 0.05 below $3.00 / 0.10 at-or-above;
        # ~5-pt strikes near the money; GTH exists but this bot trades RTH
        # 9:30-16:00 (unchanged). Verified 2026-07-21, source:
        # cdn.cboe.com/resources/spx/spx-fact-sheet.pdf.
        expiring_last_trade=time(16, 0),
        session_open=time(9, 30),
        session_close=time(16, 0),
        tick_rungs=_INDEX_TICKS,
        strike_increment_note="~5-pt strikes near the money (spx-fact-sheet.pdf)",
        daily_0dte=True,
        margin_model="cash_settled_defined_risk_spread",
        # UND-02 liquidity defaults -- the CURRENT doc 06 globals, UNCHANGED
        # ("SPX values unchanged", ratified text): target $3.00, wing 50,
        # short floor $1.00, net floor $2.00, probe_down_max 25,
        # completeness 90%, min validated 10.
        default_target_premium=Decimal("3.00"),
        default_wing_width=Decimal("50"),
        default_min_short_premium=Decimal("1.00"),
        default_min_total_credit=Decimal("2.00"),
        default_probe_down_max=25,
        default_chain_completeness_pct=Decimal("90"),
        default_min_validated_strikes=10,
        # FIX-5: SPX's index publishes a two-sided dxfeed Quote (SPXW cert
        # probe passes on quote mid -- 2026-07-21 triage evidence).
        spot_event_hint="quote",
        enabled=True,
    ),
    "RUT": UnderlyingProfile(
        name="RUT",
        index_symbol="RUT",
        option_root="RUTW",
        instrument_class="cash_index",
        multiplier=Decimal("100"),
        settlement="cash",
        # RUT: x100/pt; cash-settled European; RUTW root = PM-settled weeklys
        # with DAILY Mon-Fri expiries SINCE 2024-01-08 (the RUT specifications
        # page prose is stale -- the Cboe press release is authoritative);
        # expiring RUTW last trade 16:00 ET; ticks IDENTICAL to SPX (0.05/0.10
        # @ $3.00); strikes 2.5-pt below 200 / 5-pt at-or-above (RUT trades
        # ~2300 -> 5-pt governs); equity session hours (UND-05: unchanged from
        # SPX); RUT/RUTW joined GTH 2026-02-12 (this bot still trades RTH);
        # no position limits; fee table already carries RUT ($0.18). Verified
        # 2026-07-21, sources: cboe.com/tradable_products/ftse_russell/
        # russell_2000_index_options/rut_specifications/ + ir.cboe.com news
        # release 2023 "daily expiries for Russell 2000 beginning January 8
        # 2024"; ir.cboe.com GTH release 2026 (join date only, RTH unaffected).
        expiring_last_trade=time(16, 0),
        session_open=time(9, 30),
        session_close=time(16, 0),
        tick_rungs=_INDEX_TICKS,
        strike_increment_note="2.5-pt below 200 / 5-pt at-or-above (RUT ~2300 -> 5-pt governs)",
        daily_0dte=True,
        margin_model="cash_settled_defined_risk_spread",
        # UND-02 liquidity defaults -- RUT starting defaults = SPX values
        # pending operator tuning (UND-02: RUT 0DTE is thinner -- operator to
        # adjust in the UI); prefill-only, pin-at-Save applies (v1.47: a
        # saved row keeps its own concrete values forever).
        default_target_premium=Decimal("3.00"),
        default_wing_width=Decimal("50"),
        default_min_short_premium=Decimal("1.00"),
        default_min_total_credit=Decimal("2.00"),
        default_probe_down_max=25,
        default_chain_completeness_pct=Decimal("90"),
        default_min_validated_strikes=10,
        # FIX-5 (cert triage 2026-07-21, lead's read-only probe): the "RUT"
        # index's dxfeed Quote publishes NaN bid/ask (the SDK parser rejects
        # and skips the event) in the SAME session where SPXW's cert passes
        # -- FTSE Russell index dissemination differs from Cboe's SPX. RUT's
        # spot therefore comes from the index Trade event's last price; the
        # Quote path stays subscribed regardless (hint orders preference,
        # never disables the fallback).
        spot_event_hint="trade",
        enabled=True,
    ),
    "/ES": UnderlyingProfile(
        name="/ES",
        index_symbol="/ES",
        option_root="EW",       # NOT verified this phase -- placeholder only, see UND-03 note below
        instrument_class="futures_option",
        # UND-01/02: the ONLY /ES fact ratified in spec text this phase is the
        # x50 multiplier. Every other /ES field below is an unverified
        # placeholder — the profile is `enabled=False` precisely so nothing
        # can trade against these numbers before the UND-03 phase verifies
        # them for real (force-close-before-settlement invariant required).
        multiplier=Decimal("50"),
        settlement="physical_futures_assignment",  # UND-03: exercise assigns a future -- never held to settlement
        expiring_last_trade=time(16, 0),   # UNVERIFIED placeholder
        session_open=time(9, 30),          # UND-05: entry window runs the equity day regardless of /ES's ~23h session
        session_close=time(16, 0),
        tick_rungs=_INDEX_TICKS,           # UNVERIFIED placeholder
        strike_increment_note="pending UND-03 verification",
        daily_0dte=False,                  # UNVERIFIED this phase
        margin_model="futures_defined_risk_pending_verification",
        enabled=False,
        disabled_reason="UND-03 /ES phase not yet built — force-close invariant required",
    ),
}


def profile_for(name: str | None, default: str = "SPX") -> UnderlyingProfile | None:
    """UND-01: resolve a profile by name. `name` unset (None/empty) defaults
    to `default` (SPX) — behaviour byte-identical to pre-v1.86 when the
    caller never named an underlying at all. A name that is not a key of
    `PROFILES` at all (e.g. "XSP") returns None — REFUSED, never guessed;
    callers (config validation, schedule validation) decide the reason. A
    KNOWN but DISABLED profile ("/ES" this phase) is still returned — pure
    lookup, not a policy decision; callers that must refuse a disabled
    underlying check `.enabled` and report `.disabled_reason` themselves
    (see `domain/schedule.py::validate_entry`)."""
    key = name if name else default
    return PROFILES.get(key)


def profile_by_root(option_root: str) -> UnderlyingProfile | None:
    """The inverse lookup: given an OCC option root (e.g. an `OrderIntent`'s
    `underlying` field, "SPXW"/"RUTW"), find the profile it belongs to. Used
    where only the ROOT is in hand (e.g. a broker fill/order intent) and the
    profile's INDEX-keyed facts (multiplier, fee table lookup) are needed.
    None for a root that matches no known profile — never guessed."""
    for profile in PROFILES.values():
        if profile.option_root == option_root:
            return profile
    return None
