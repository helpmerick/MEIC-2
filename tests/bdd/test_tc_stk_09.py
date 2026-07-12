"""Hand-written step definitions for TC-STK-09 — STK-10 v1.55 baseline
pre-validation (spec/01-strategy-rules.md STK-10 baseline pre-validation
paragraph; spec/04-test-cases.md TC-STK-09).

v1.55 completes the v1.51 trade-relative rework: the entry's VALIDATED
UNIVERSE is captured ONCE (at ENT-08 warm-up, or at manual-entry press) —
`domain/chain.py: validated_universe`. Thereafter, fire-time completeness
measures REGRESSION from that known-good picture (never a fresh reachable-set
recompute), selection shops only inside it (`domain/walk.py: select_side`'s
`validated=` parameter), and a sliver baseline (< `min_validated_strikes` per
side) alerts and retries rather than ever trivially passing.

`LiveCondorSelector(..., baseline_pre_validation=True)` is the OPT-IN switch
(composition/live_selection.py) — every pre-v1.55 caller that omits it (every
existing test) keeps the untouched v1.51 per-attempt recompute.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.composition.live_selection import LiveCondorSelector, SelectionConfig
from meic.domain.chain import ChainSide, Mark, completeness_ok, reachable_strikes, validated_universe
from meic.domain.walk import Selected, Skip, select_side
from tests.harness.fake_clock import ET, FakeClock

scenarios("../features/TC-STK-09.feature")

SPOT = D("6000")
START = datetime(2026, 7, 8, 10, 0, tzinfo=ET)
CFG = SelectionConfig(target_premium=D("3.00"), wing_width=D("50"),
                      min_short_premium=D("1.00"), min_total_credit=D("2.00"),
                      completeness_pct=D("90"), min_validated_strikes=10)


def _mk(mid: str) -> Mark:
    m = D(mid)
    return Mark(bid=m - D("0.05"), ask=m + D("0.05"))


def _healthy_side(direction: D, n: int = 25) -> ChainSide:
    strikes = tuple(SPOT + direction * D(5 * i) for i in range(n))
    curve = lambda i: max(D("0.15"), D("3.60") - D("0.30") * i)
    marks = {s: _mk(str(curve(i))) for i, s in enumerate(strikes)}
    return ChainSide(strikes, marks)


def _reachable(side: ChainSide, direction: D) -> frozenset:
    return reachable_strikes(side, target_premium=CFG.target_premium, wing_width=CFG.wing_width,
                             otm_direction=direction, min_short_premium=CFG.min_short_premium)


class Snap:
    """Stand-in for ChainSnapshot — only what LiveCondorSelector reads."""

    def __init__(self, put_side, call_side, stale=False):
        self.put_side, self.call_side, self.stale = put_side, call_side, stale


def _drive_to_completion(selector: LiveCondorSelector, clock: FakeClock, when: datetime,
                         *, max_steps: int = 12, step_seconds: float = 5):
    async def run():
        task = asyncio.ensure_future(selector(when, 1))
        for _ in range(max_steps):
            await asyncio.sleep(0)
            if task.done():
                break
            clock.advance(step_seconds)
        return await task
    return asyncio.run(run())


@pytest.fixture
def world():
    return {}


# --- Scenario: Dead-at-baseline strikes never count as holes -----------------

@given('warm-up validates 24 of 28 reachable strikes (4 far wings listed but never quoted)')
def _(world):
    full = _healthy_side(D(-1))
    reachable = _reachable(full, D(-1))
    # the 4 FARTHEST-OTM reachable strikes are wing/shift-budget additions that
    # were never actually quoted — dead at baseline, by construction (never
    # required a mark to be counted reachable in the first place).
    dead = set(sorted(reachable)[:4])
    baseline_side = ChainSide(full.strikes_toward_otm,
                              {k: v for k, v in full.marks.items() if k not in dead})
    validated = validated_universe(baseline_side, reachable)
    world["full"], world["reachable"], world["dead"] = full, reachable, dead
    world["full_call"] = _healthy_side(D(1))     # a healthy call side, always fully reachable
    world["baseline_side"] = baseline_side
    world["validated"] = validated
    # this fixture's own numbers (illustrative 24/28 in the spec prose; ours:
    # 19 reachable, 4 dead-at-baseline, 15 validated) -- self-consistent below.
    assert len(reachable) - len(dead) == len(validated)


@given('at fire time 23 of the 24 validated strikes are still fresh')
def _(world):
    validated = world["validated"]
    # one validated strike (closest to the money, arbitrary) goes stale by fire
    # time -- everything else (including the 4 permanently dead ones) unchanged.
    stale_one = sorted(validated, reverse=True)[0]
    world["stale_one"] = stale_one
    fire_side = ChainSide(world["full"].strikes_toward_otm,
                          {k: v for k, v in world["baseline_side"].marks.items() if k != stale_one})
    world["fire_side"] = fire_side


@then('completeness = 95.8 percent and the gate PASSES')
def _(world):
    validated = world["validated"]
    still_fresh = sum(1 for s in validated if world["fire_side"].is_marked(s))
    pct = D(still_fresh) / D(len(validated)) * 100
    assert still_fresh == len(validated) - 1
    assert pct >= CFG.completeness_pct   # this fixture: 14/15 = 93.3% >= 90% -- PASSES
    assert completeness_ok(world["fire_side"], reachable=validated,
                           completeness_pct=CFG.completeness_pct) is True

    # end-to-end through the selector: the FIRST attempt (a healthy snapshot)
    # locks the baseline; a LATER attempt reading the (now slightly stale)
    # fire_side is judged against that LOCKED baseline, not a fresh recompute
    # -- exactly the "captured once, reused" property v1.55 requires.
    async def clean_provider():
        return Snap(world["baseline_side"], world["full_call"])

    async def fire_provider():
        return Snap(world["fire_side"], world["full_call"])

    selector = LiveCondorSelector(snapshot_provider=clean_provider, config=CFG,
                                  baseline_pre_validation=True)
    first_condor, first_reason = asyncio.run(selector._attempt(CFG, when=START, entry_number=1))
    assert first_condor is not None and first_reason is None   # baseline locked, clean fire
    assert selector._baseline is not None
    locked_put_validated = selector._baseline.put

    selector.snapshot_provider = fire_provider   # simulate time passing: one strike goes stale
    condor, reason = asyncio.run(selector._attempt(CFG, when=START, entry_number=1))
    assert condor is not None and reason is None      # STILL passes: baseline unchanged, reused
    assert selector._baseline.put == locked_put_validated  # never re-derived from fire_side


@then('under the pre-v1.55 rule the same day would have falsely skipped at 85.7 percent')
def _(world):
    # the OLD (v1.51) rule recomputes the reachable set fresh and never
    # distinguishes "dead at baseline" from "genuinely regressed" -- at the
    # very baseline moment itself (nothing has even gone stale yet), it would
    # already measure validated/reachable and fail the 90% floor. This
    # fixture's own numbers: 15/19 = 78.9%, well under 90% -- a false skip on
    # an otherwise perfectly healthy day, exactly the v1.51 defect v1.55 fixes.
    old_pct = D(len(world["validated"])) / D(len(world["reachable"])) * 100
    assert old_pct < CFG.completeness_pct
    assert completeness_ok(world["baseline_side"], reachable=world["reachable"],
                           completeness_pct=CFG.completeness_pct) is False


# --- Scenario: A genuine feed regression still fails --------------------------

@given('warm-up validated 24 strikes and only 12 remain fresh at fire time')
def _(world):
    full = _healthy_side(D(-1))
    reachable = _reachable(full, D(-1))
    dead = set(sorted(reachable)[:4])
    baseline_side = ChainSide(full.strikes_toward_otm,
                              {k: v for k, v in full.marks.items() if k not in dead})
    validated = validated_universe(baseline_side, reachable)
    # a genuine regression: well under half of the validated universe stays
    # fresh, and it NEVER heals within the entry window.
    keep_fresh = set(sorted(validated, reverse=True)[:7])   # 7 of 15 -- 46.7%
    regressed_side = ChainSide(full.strikes_toward_otm,
                               {k: v for k, v in baseline_side.marks.items() if k in keep_fresh})
    assert completeness_ok(regressed_side, reachable=validated,
                           completeness_pct=CFG.completeness_pct) is False
    world["baseline_side"], world["regressed_side"] = baseline_side, regressed_side
    world["full_call"] = _healthy_side(D(1))


@then('the gate fails and the entry retries then skips incomplete_chain')
def _(world):
    # baseline was captured earlier (warm-up, a clean snapshot) and is
    # already locked; the chain has since regressed and NEVER heals within
    # the entry window -- every fire-time attempt reads the same regressed
    # snapshot, judged against the (unchanged) locked baseline.
    async def clean_provider():
        return Snap(world["baseline_side"], world["full_call"])

    selector = LiveCondorSelector(snapshot_provider=clean_provider, config=CFG,
                                  baseline_pre_validation=True)
    locked_condor, locked_reason = asyncio.run(
        selector._attempt(CFG, when=START, entry_number=1))
    assert locked_condor is not None and locked_reason is None
    assert selector._baseline is not None

    clock = FakeClock(START)
    calls = {"n": 0}

    async def regressed_provider():
        calls["n"] += 1
        return Snap(world["regressed_side"], world["full_call"])

    selector.clock = clock
    selector.entry_window_seconds = 20
    selector.chain_retry_seconds = 5
    selector.snapshot_provider = regressed_provider
    condor, reason = _drive_to_completion(selector, clock, START)
    assert condor is None and reason == "incomplete_chain"
    assert calls["n"] > 1   # it actually retried, not a single-shot skip


# --- Scenario: A sliver baseline cannot trivially pass ------------------------

@given('warm-up finds only 5 validated strikes on the call side with min_validated_strikes = 10')
def _(world):
    full_put = _healthy_side(D(-1))
    full_call = _healthy_side(D(1))
    call_reachable = _reachable(full_call, D(1))
    # strip the call side down to a 5-strike sliver -- well under the floor.
    sliver_call_marked = set(sorted(call_reachable)[:5])
    sliver_call = ChainSide(full_call.strikes_toward_otm,
                            {k: v for k, v in full_call.marks.items() if k in sliver_call_marked})
    world["snap"] = Snap(full_put, sliver_call)


@then('a warm-up alert fires 60 seconds before the window and the entry retries')
def _(world):
    alerts: list[tuple[str, str]] = []
    clock = FakeClock(START)

    async def provider():
        return world["snap"]

    selector = LiveCondorSelector(snapshot_provider=provider, config=CFG, clock=clock,
                                  entry_window_seconds=10, chain_retry_seconds=5,
                                  baseline_pre_validation=True,
                                  alert=lambda level, msg: alerts.append((level, msg)))
    condor, reason = _drive_to_completion(selector, clock, START)
    world["condor"], world["reason"] = condor, reason
    assert alerts, "a sliver baseline must alert (v1.55(3) viability floor)"
    assert alerts[0][0] == "warning"
    assert "sliver" in alerts[0][1]


@then('an unhealed baseline skips incomplete_chain')
def _(world):
    assert world["condor"] is None and world["reason"] == "incomplete_chain"


# --- Scenario: A dead wing is a candidate skip, not an entry failure ----------

@given('a candidate short whose wing strike is not in the validated universe')
def _(world):
    # a minimal hand-built side: TWO probe-matching shorts (6000 exact target,
    # then 5950 as the next probe) so the walk has somewhere to fall back to.
    # 6000's wing (5950, width 50) is deliberately EXCLUDED from `validated`
    # (dead-at-baseline) even though it's LISTED and MARKED right now -- this
    # isolates "not validated" from "no mark" (STK-11 WingUnmarked) and from
    # "not listed" (STK-07 no_valid_strikes).
    side = ChainSide(
        (D("6000"), D("5990"), D("5950"), D("5940")),
        {D("6000"): _mk("3.00"), D("5990"): _mk("3.15"),
         D("5950"): _mk("0.50"), D("5940"): _mk("0.45")})
    # validated universe includes the SHORTS (6000, 5990) and the SECOND
    # short's own wing (5940) -- but NOT the first short's wing (5950).
    validated = frozenset({D("6000"), D("5990"), D("5940")})
    world["side"], world["validated"] = side, validated


@then('that candidate is skipped and the probe walk continues')
def _(world):
    result = select_side(world["side"], target_premium=D("3.00"), wing_width=D("50"),
                         otm_direction=D(-1), min_short_premium=D("1.00"),
                         validated=world["validated"])
    # 6000 (probe #1, exact target) is rejected -- its wing (5950) is not
    # validated -- so the walk falls through to 5990 instead of failing.
    assert isinstance(result, Selected)
    assert result.short_strike == D("5990")


@then('the entry fails only if no valid candidate remains')
def _(world):
    # strip the fallback (5990/5940) out of validated too -- now NO candidate
    # has a validated wing, so the walk must exhaust and fail, never guess.
    validated = frozenset({D("6000"), D("5990")})   # neither wing validated
    result = select_side(world["side"], target_premium=D("3.00"), wing_width=D("50"),
                         otm_direction=D(-1), min_short_premium=D("1.00"),
                         validated=validated)
    assert isinstance(result, Skip) and result.reason == "no_valid_strikes"


# --- Scenario: Manual entries baseline at press -------------------------------

@given('the operator fires manually with no warm-up')
def _(world):
    world["snap"] = Snap(_healthy_side(D(-1)), _healthy_side(D(1)))


@then('the validated universe is captured at press time under the same rules')
def _(world):
    # ENT-09 manual fires call the SAME selector directly, with no separate
    # earlier warm-up phase -- the very first attempt IS "at press", and
    # `baseline_pre_validation` captures the baseline from it exactly once,
    # under the identical rules (reachable set + viability floor) as a
    # scheduled entry's warm-up would.
    calls = {"n": 0}

    async def provider():
        calls["n"] += 1
        return world["snap"]

    selector = LiveCondorSelector(snapshot_provider=provider, config=CFG,
                                  baseline_pre_validation=True)
    condor, reason = asyncio.run(selector(START, 1))
    assert condor is not None and reason is None
    assert calls["n"] == 1                     # one snapshot: capture AND fire, at press
    assert selector._baseline is not None      # the baseline is now locked for this entry
