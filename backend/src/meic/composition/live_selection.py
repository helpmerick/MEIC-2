"""LiveCondorSelector — turn a live chain snapshot into a Condor, or a skip reason.

Composes already-tested pure domain pieces in the spec's order:
  DAT-02 freshness -> STK-10 reachable-set completeness -> probe walk
  (STK-02/11) -> STK-09 collisions -> credit gates re-run on the FINAL
  strikes (STK-05/06).

Every failure returns a NAMED skip reason and no Condor: the runtime then skips
the entry. Nothing here can produce a partially-valid selection — a missing mark
on any final leg is a skip, not a guess.

STK-10 v1.51 retry: an "incomplete_chain" gate failure or a missing wing at
selection (`wing_unmarked`) does not skip immediately — it retries every
`chain_retry_seconds`, taking a FRESH snapshot each time, until `when +
entry_window_seconds` (ENT-02). Only then does it become a real skip. Every
other failure (stale data, no valid strikes, a strike collision, a credit-gate
miss) is terminal on the first attempt. `clock=None` (the default, and every
existing unit test) disables retrying entirely — a single attempt, exactly the
pre-v1.51 behavior — so retrying is opt-in via composition, never implicit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping

from meic.application.execute_entry import Condor
from meic.application.market_calendar import trading_day
from meic.domain.chain import ChainSide, completeness_ok, reachable_strikes, validated_universe
from meic.domain.collision import Abort, Resolved, resolve_collisions
from meic.domain.gates import GatesFailed, check_credit_gates
from meic.domain.walk import Selected, Skip, WingUnmarked, select_side

Occupancy = Mapping[Decimal, frozenset]

# STK-10 v1.51: these two skip reasons are RETRYABLE within the entry window —
# everything else (data_unavailable, no_valid_strikes, strike_collision,
# insufficient_credit/insufficient_premium, ...) is terminal on first attempt.
_RETRYABLE_REASONS = frozenset({"incomplete_chain", "wing_unmarked"})


@dataclass(frozen=True)
class SelectionConfig:
    target_premium: Decimal = Decimal("3.00")
    wing_width: Decimal = Decimal("50")
    min_short_premium: Decimal = Decimal("1.00")
    min_total_credit: Decimal = Decimal("2.00")
    completeness_pct: Decimal = Decimal("90")
    # STK-10 v1.55 baseline viability floor (doc 06: range 3-40, default 10).
    # CHAIN-scoped like `completeness_pct` above (describes the chain, not the
    # row) -- `_attempt` always reads it off `self.config`, never a per-row `c`.
    min_validated_strikes: int = 10
    contracts: int = 1                        # ENT-04 (v1.44): this row's own size

    @classmethod
    def for_entry(cls, entry, *, completeness_pct: Decimal = Decimal("90")) -> "SelectionConfig":
        """Build a per-ROW selection config from a ResolvedEntry (doc 06 §37).

        Every one of these is a per-entry override, so selection MUST run against
        the row's values — a row asking for a 30-wide wing at $2.00 must not be
        selected against the global 50-wide/$3.00 defaults and then traded.
        `completeness_pct` is not per-entry (it describes the chain, not the row).
        """
        return cls(target_premium=entry.target_premium, wing_width=entry.wing_width,
                   min_short_premium=entry.min_short_premium,
                   min_total_credit=entry.min_total_credit,
                   completeness_pct=completeness_pct, contracts=entry.contracts)


def floor_candidates(snap: Any, config: SelectionConfig) -> dict:
    """ENT-09b v1.57: the manual-fire dialog's floor dropdown data -- per-side
    candidate strikes from the entry's live VALIDATED UNIVERSE (v1.55:
    `reachable_strikes` intersected with strikes carrying a fresh two-sided
    mark RIGHT NOW, at dialog population), each with its distance from spot
    (points) and live mid, plus spot + the quote timestamp.

    Pure given `snap` (a ChainSnapshot-shaped object: `.put_side`, `.call_side`,
    `.spot`, `.stale`, `.taken_at`) and `config` (the row's SelectionConfig) --
    no I/O, independently testable. `snap is None` or `snap.stale` -> no
    candidates (the dialog cannot populate from data that isn't fresh).
    """
    if snap is None or snap.stale:
        return {"put": [], "call": [], "spot": None, "quote_at": None}

    def _side(chain: ChainSide, direction: Decimal) -> list[dict]:
        reachable = reachable_strikes(
            chain, target_premium=config.target_premium, wing_width=config.wing_width,
            otm_direction=direction, min_short_premium=config.min_short_premium)
        validated = validated_universe(chain, reachable)
        spot = snap.spot
        rows = [
            {"strike": str(s), "distance_pts": str(abs(s - spot)), "mid": str(chain.marks[s].mid)}
            for s in validated
        ]
        # puts descending from the money, calls ascending (ENT-09b v1.57 UI)
        rows.sort(key=lambda r: Decimal(r["strike"]), reverse=(direction < 0))
        return rows

    taken_at = getattr(snap, "taken_at", None)
    return {
        "put": _side(snap.put_side, Decimal(-1)),
        "call": _side(snap.call_side, Decimal(1)),
        "spot": str(snap.spot) if snap.spot is not None else None,
        "quote_at": taken_at.isoformat() if taken_at else None,
    }


@dataclass(frozen=True)
class _Baseline:
    """STK-10 v1.55: one entry's locked validated universe (see `validated_universe`)."""

    put: frozenset[Decimal]
    call: frozenset[Decimal]


@dataclass
class LiveCondorSelector:
    snapshot_provider: Callable[[], Awaitable]        # () -> ChainSnapshot
    config: SelectionConfig = SelectionConfig()
    occupancy_provider: Callable[[], Occupancy] = dict
    # STK-10 v1.51 retry (doc 06 chain_retry_seconds/entry window, ENT-02). A
    # `None` clock (the default) means "no retry" — every pre-v1.51 unit test
    # that doesn't pass a clock keeps its single-attempt behavior unchanged.
    clock: Any = None                                  # Clock port: now() + wait_until()
    entry_window_seconds: int = 120                    # ENT-02 default (doc 06)
    chain_retry_seconds: int = 5                       # STK-10 default (doc 06)
    # STK-10 v1.55 baseline pre-validation. OPT-IN, like `clock` above: `False`
    # (the default) keeps every pre-v1.55 caller on the untouched v1.51
    # per-attempt reachable-set recompute. When `True`, the FIRST capture for
    # a given (when, entry_number) locks the validated universe, and every
    # later attempt within the entry window (including the eventual fire)
    # reuses that SAME locked baseline rather than recomputing it.
    #
    # Scheduled entries (operator ruling 2026-07-11, closing the v1.55 gap):
    # the ENT-08 warm-up (server.py `_wire_live_day`'s real warm-up wiring)
    # calls `warm_baseline` at T-60 with the SAME (when, entry_number) key the
    # fire will use, so the baseline is ALREADY locked before the fire's first
    # attempt ever runs -- there is no longer a fire-time approximation for
    # scheduled entries. Manual ENT-09 entries have no warm-up step, so their
    # first capture genuinely happens at press ("at press" per spec, not an
    # approximation of anything). See `_locked_baseline`/`warm_baseline` below.
    baseline_pre_validation: bool = False
    # RSK-06-style alert sink for the v1.55(3) viability floor (sliver
    # baseline): `alert(level, message)`. `None` is a valid no-op (tests that
    # don't care about alerting never need to supply one).
    alert: Callable[[str, str], None] | None = None
    _baseline_key: tuple | None = field(default=None, repr=False, compare=False)
    _baseline: "_Baseline | None" = field(default=None, repr=False, compare=False)

    def _side(self, chain: ChainSide, direction: Decimal, c: SelectionConfig,
              *, validated: frozenset[Decimal] | None = None,
              short_floor: Decimal | None = None):
        return select_side(chain, target_premium=c.target_premium, wing_width=c.wing_width,
                           otm_direction=direction, min_short_premium=c.min_short_premium,
                           validated=validated, short_floor=short_floor)

    def _resolve(self, sel: Selected, chain: ChainSide, direction: Decimal, occ: Occupancy,
                 c: SelectionConfig):
        return resolve_collisions(
            short_strike=sel.short_strike, long_strike=sel.long_strike, occupancy=occ,
            listed_strikes_toward_otm=chain.strikes_toward_otm,
            wing_width=c.wing_width, otm_direction=direction)

    async def __call__(self, when: datetime, entry_number: int,
                       config: SelectionConfig | None = None, *,
                       put_floor: Decimal | None = None,
                       call_floor: Decimal | None = None) -> tuple[Condor | None, str | None]:
        """`config` overrides the global one for THIS row (ENT-04 / doc 06 §37).

        `put_floor`/`call_floor` (ENT-09b v1.57): manual-entry-only minimum
        short-strike floors, per-press (never persisted schedule state) --
        `None` (the default) is every non-manual and pre-v1.57 caller.

        STK-10 v1.51: retries every `chain_retry_seconds` (fresh snapshot each
        time) while the failure is `incomplete_chain` or `wing_unmarked`, until
        `when + entry_window_seconds` — then returns that same reason. Every
        other reason returns on the first attempt.
        """
        c = config or self.config
        deadline = when + timedelta(seconds=self.entry_window_seconds)

        while True:
            condor, reason = await self._attempt(
                c, when=when, entry_number=entry_number,
                put_floor=put_floor, call_floor=call_floor)
            if condor is not None:
                return condor, None
            if reason not in _RETRYABLE_REASONS or self.clock is None:
                return None, reason
            now = self.clock.now()
            if now >= deadline:
                return None, reason               # window expired, still unhealed
            next_try = min(now + timedelta(seconds=self.chain_retry_seconds), deadline)
            await self.clock.wait_until(next_try)

    def _locked_baseline(self, snap, c: SelectionConfig, *,
                         when: datetime, entry_number: int) -> tuple["_Baseline | None", str | None]:
        """STK-10 v1.55: return the locked baseline for (when, entry_number),
        (re-)capturing it from THIS attempt's fresh snapshot if none is locked
        yet. A new (when, entry_number) key always starts a fresh baseline --
        a previous entry's lock must never leak into the next one.

        Viability floor (3): each side's validated universe must hold >=
        `config.min_validated_strikes` candidates, else this is a SLIVER
        baseline -- alert, and return `(None, "incomplete_chain")`, which
        plugs directly into the EXISTING retryable-reason loop in `__call__`:
        every subsequent attempt (every `chain_retry_seconds`, until the entry
        window closes) re-tries capture from a fresh snapshot, exactly like
        v1.51's own incomplete-chain retry. Once viable, the baseline LOCKS
        and every later attempt (including ones with an unrelated terminal
        failure retried for other reasons) reuses it unchanged.
        """
        key = (when, entry_number)
        if self._baseline_key != key:
            self._baseline_key = key
            self._baseline = None
        if self._baseline is not None:
            return self._baseline, None

        put_reachable = reachable_strikes(
            snap.put_side, target_premium=c.target_premium, wing_width=c.wing_width,
            otm_direction=Decimal(-1), min_short_premium=c.min_short_premium)
        call_reachable = reachable_strikes(
            snap.call_side, target_premium=c.target_premium, wing_width=c.wing_width,
            otm_direction=Decimal(1), min_short_premium=c.min_short_premium)
        put_validated = validated_universe(snap.put_side, put_reachable)
        call_validated = validated_universe(snap.call_side, call_reachable)

        floor = self.config.min_validated_strikes  # CHAIN-scoped, like completeness_pct
        if len(put_validated) < floor or len(call_validated) < floor:
            if self.alert is not None:
                self.alert("warning",
                          f"STK-10 v1.55: sliver baseline (put={len(put_validated)}, "
                          f"call={len(call_validated)}, floor={floor}) — retrying")
            return None, "incomplete_chain"

        self._baseline = _Baseline(put=put_validated, call=call_validated)
        return self._baseline, None

    def warm_baseline(self, snap, config: SelectionConfig | None, *,
                      when: datetime, entry_number: int) -> None:
        """ENT-08 (v1.55 hook, operator ruling 2026-07-11): called by the real
        warm-up wiring at T-60, with the SAME (when, entry_number) key the
        eventual fire will use -- so the fire's first `_attempt` finds this
        entry's baseline already locked instead of capturing it lazily.

        A no-op when baseline pre-validation is off, or `snap` is missing/
        stale (nothing honest to lock from yet -- the fire's own retry loop
        is what handles that, exactly as it does when no warm-up ran at all).
        Sliver detection/alerting is unchanged: `_locked_baseline` still
        alerts and returns `(None, "incomplete_chain")` on a too-thin universe,
        it simply does not lock -- the fire retries the capture, same as
        v1.55's per-attempt behavior."""
        if not self.baseline_pre_validation or snap is None or snap.stale:
            return
        c = config or self.config
        self._locked_baseline(snap, c, when=when, entry_number=entry_number)

    async def _attempt(self, c: SelectionConfig, *, when: datetime,
                       entry_number: int, put_floor: Decimal | None = None,
                       call_floor: Decimal | None = None) -> tuple[Condor | None, str | None]:
        """One selection pass against a FRESH snapshot — no retrying here."""
        snap = await self.snapshot_provider()

        if snap.stale:                                    # DAT-02: never trade stale data
            return None, "data_unavailable"

        put_validated: frozenset[Decimal] | None = None
        call_validated: frozenset[Decimal] | None = None

        if self.baseline_pre_validation:
            baseline, reason = self._locked_baseline(snap, c, when=when, entry_number=entry_number)
            if baseline is None:
                return None, reason                        # sliver baseline (v1.55) -- retryable
            put_validated, call_validated = baseline.put, baseline.call
            # STK-10 v1.55: fire-time completeness is measured against the LOCKED
            # baseline still fresh -- never a freshly recomputed reachable set.
            for chain, validated in ((snap.put_side, put_validated), (snap.call_side, call_validated)):
                if not completeness_ok(chain, reachable=validated,
                                       completeness_pct=self.config.completeness_pct):
                    return None, "incomplete_chain"
        else:
            # STK-10 v1.51 (pre-baseline): the entry's own TRADE-RELATIVE
            # reachable strike set, never a fixed ATM band. completeness_pct is
            # CHAIN-scoped, never per-row (`for_entry` docstring) — the gate
            # reads THIS selector's config, not the per-entry override, which
            # `for_entry` builds with a hardcoded default the wiring can't
            # reach. This is what lets the composition wire doc 06's
            # `chain_completeness_pct` dial. The reachable set itself IS
            # per-row (target/wing/floor are the row's own parameters), so
            # it's computed from `c`.
            for chain, direction in ((snap.put_side, Decimal(-1)), (snap.call_side, Decimal(1))):
                reachable = reachable_strikes(
                    chain, target_premium=c.target_premium, wing_width=c.wing_width,
                    otm_direction=direction, min_short_premium=c.min_short_premium)
                if not completeness_ok(chain, reachable=reachable,
                                       completeness_pct=self.config.completeness_pct):
                    return None, "incomplete_chain"

        put = self._side(snap.put_side, Decimal(-1), c, validated=put_validated, short_floor=put_floor)
        call = self._side(snap.call_side, Decimal(1), c, validated=call_validated, short_floor=call_floor)
        for r in (put, call):
            if isinstance(r, Skip):
                return None, r.reason
            if isinstance(r, WingUnmarked):
                return None, "wing_unmarked"             # retryable (STK-10 v1.51)

        occ = self.occupancy_provider() or {}
        legs: dict[str, Resolved] = {}
        for name, sel, chain, direction in (
            ("put", put, snap.put_side, Decimal(-1)),
            ("call", call, snap.call_side, Decimal(1)),
        ):
            resolved = self._resolve(sel, chain, direction, occ, c)
            if isinstance(resolved, Abort):
                return None, resolved.reason             # strike_collision
            legs[name] = resolved

        # Every FINAL leg must carry a real mark — a shifted strike without one
        # is a skip, never an estimate.
        mids: dict[str, Decimal] = {}
        for name, chain in (("put", snap.put_side), ("call", snap.call_side)):
            for role, strike in (("short", legs[name].short_strike), ("long", legs[name].long_strike)):
                mark = chain.marks.get(strike)
                if mark is None:
                    return None, "wing_unmarked" if role == "long" else "no_valid_strikes"
                mids[f"{name}_{role}"] = mark.mid

        net_credit = ((mids["put_short"] - mids["put_long"]) +
                      (mids["call_short"] - mids["call_long"]))

        # STK-05/06 re-run on the final (possibly shifted) strikes
        gate = check_credit_gates(
            put_short_mid=mids["put_short"], call_short_mid=mids["call_short"],
            total_net_credit_mid=net_credit,
            min_short_premium=c.min_short_premium, min_total_credit=c.min_total_credit)
        if isinstance(gate, GatesFailed):
            return None, gate.reason

        return Condor(
            entry_number=entry_number,
            put_short=legs["put"].short_strike, call_short=legs["call"].short_strike,
            # STK-03 wings — the ACL needs all four strikes to build the order.
            put_long=legs["put"].long_strike, call_long=legs["call"].long_strike,
            put_short_mid=mids["put_short"], call_short_mid=mids["call_short"],
            mid_credit=net_credit, min_total_credit=c.min_total_credit,
            expiration=trading_day(when),  # 0DTE (DAY-03: the ET trading day `when`
            # falls on, not `when`'s own tzinfo's raw `.date()` -- `when` can be a
            # UTC-aware clock reading (manual ENT-09 fire), and only ever agreeing
            # with the ET date by luck of always firing inside market hours).
            contracts=c.contracts,   # ENT-04 (v1.44): the ROW's size, not a global knob
        ), None
