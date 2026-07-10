"""Hand-written step definitions for TC-STK-07 — STK-10 chain integrity (Phase 3).

v1.51 NOTE: STK-10 is now TRADE-RELATIVE (spec/01-strategy-rules.md STK-10):
before selection, the gate inspects each entry's own REACHABLE strike set —
marked strikes whose rounded mid falls in the probe premium window, their
wings, and the STK-09 shift-budget extensions (domain/chain.py:
`reachable_strikes`) — never a fixed `chain_atm_band_pts` (RETIRED, config
validation rejects the key). The retry cadence (`chain_retry_seconds` within
the entry window) is implemented INSIDE `LiveCondorSelector` itself
(composition/live_selection.py) and is driven here with a real `FakeClock` —
never `time.sleep`.
"""
import asyncio
from datetime import datetime
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.composition.live_selection import LiveCondorSelector, SelectionConfig
from meic.config.validation import ConfigRejected, validate_config
from meic.domain.chain import ChainSide, Mark, completeness_ok, reachable_strikes
from meic.domain.walk import Selected, WingUnmarked, select_side
from tests.harness.fake_clock import ET, FakeClock

scenarios("../features/TC-STK-07.feature")

WALK = dict(target_premium=D("3.00"), wing_width=D("50"), otm_direction=D(-1))
CFG = SelectionConfig(target_premium=D("3.00"), wing_width=D("50"),
                      min_short_premium=D("1.00"), min_total_credit=D("2.00"),
                      completeness_pct=D("90"))

SPOT = D("6000")
START = datetime(2026, 7, 8, 10, 0, tzinfo=ET)

# The healthy curve's own probe-walk result (pinned so the scenarios below can
# assert on it without re-deriving): short 5990 / wing 5940 (put side).
SHORT_STRIKE = D("5990")
WING_STRIKE = D("5940")


def _mk(mid: str) -> Mark:
    m = D(mid)
    return Mark(bid=m - D("0.05"), ask=m + D("0.05"))


def _healthy_side(direction: D, n: int = 25) -> ChainSide:
    """A decaying-premium curve like a real 0DTE chain: i=2 sits at the 3.00
    target (matches the top-level SHORT_STRIKE/WING_STRIKE pin above)."""
    strikes = tuple(SPOT + direction * D(5 * i) for i in range(n))
    curve = lambda i: max(D("0.15"), D("3.60") - D("0.30") * i)
    marks = {s: _mk(str(curve(i))) for i, s in enumerate(strikes)}
    return ChainSide(strikes, marks)


class Snap:
    """Stand-in for ChainSnapshot — only what LiveCondorSelector reads."""

    def __init__(self, put_side, call_side, stale=False):
        self.put_side, self.call_side, self.stale = put_side, call_side, stale


def _healthy_snapshot(**over) -> Snap:
    return Snap(_healthy_side(D(-1)), _healthy_side(D(1)), **over)


def _reachable(side: ChainSide, direction: D) -> frozenset:
    return reachable_strikes(side, target_premium=CFG.target_premium, wing_width=CFG.wing_width,
                             otm_direction=direction, min_short_premium=CFG.min_short_premium)


def _drive_to_completion(selector: LiveCondorSelector, clock: FakeClock, when: datetime,
                          *, max_steps: int = 12, step_seconds: float = 5):
    """Run `selector(when, 1)` to completion, advancing `clock` by
    `step_seconds` between attempts so its internal retry loop's
    `wait_until` calls resolve — the way a real test harness drives a
    FakeClock (never `time.sleep`)."""
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


# --- Scenario: holey chain blocks, heals, proceeds ---------------------------

@given('only 75% of strikes within the ATM band have marks at fire time')
def _(world):
    full = _healthy_snapshot()
    reachable = sorted(_reachable(full.put_side, D(-1)))
    keep = int(len(reachable) * 0.75)
    strip = set(reachable[keep:])
    holey_put = ChainSide(full.put_side.strikes_toward_otm,
                          {k: v for k, v in full.put_side.marks.items() if k not in strip})
    world["full"] = full
    world["holey_put"] = holey_put
    world["reachable"] = frozenset(reachable)


@then('no strike selection occurs and the gate retries every chain_retry_seconds')
def _(world):
    # The immediate (single-attempt) verdict: below completeness_pct -> blocked.
    # The retry CADENCE itself is exercised end-to-end in the next steps.
    assert not completeness_ok(world["holey_put"], reachable=world["reachable"],
                               completeness_pct=CFG.completeness_pct)


@when('the chain completes at T+20s (within the entry window)')
def _(world):
    world["heal_after_s"] = 20


@then('selection proceeds normally')
def _(world):
    clock = FakeClock(START)
    full, holey_put, heal_after = world["full"], world["holey_put"], world["heal_after_s"]

    async def provider():
        elapsed = (clock.now() - START).total_seconds()
        put_side = full.put_side if elapsed >= heal_after else holey_put
        return Snap(put_side, full.call_side)

    selector = LiveCondorSelector(snapshot_provider=provider, config=CFG, clock=clock,
                                  entry_window_seconds=120, chain_retry_seconds=5)
    condor, reason = _drive_to_completion(selector, clock, START)
    assert condor is not None and reason is None
    assert condor.put_short == SHORT_STRIKE and condor.put_long == WING_STRIKE


# --- Scenario: persistent holes -> skip -------------------------------------

@given("the entry's trade-relative reachable strike set never reaches chain_completeness_pct within entry_window_seconds")
def _(world):
    full = _healthy_snapshot()
    reachable = sorted(_reachable(full.put_side, D(-1)))
    keep = int(len(reachable) * 0.5)   # well below 90%, and it NEVER heals
    strip = set(reachable[keep:])
    holey_put = ChainSide(full.put_side.strikes_toward_otm,
                          {k: v for k, v in full.put_side.marks.items() if k not in strip})
    world["snap"] = Snap(holey_put, full.call_side)


@then('the entry is SKIPPED with reason "incomplete_chain" and no order is submitted')
def _(world):
    clock = FakeClock(START)

    async def provider():
        return world["snap"]

    selector = LiveCondorSelector(snapshot_provider=provider, config=CFG, clock=clock,
                                  entry_window_seconds=20, chain_retry_seconds=5)
    condor, reason = _drive_to_completion(selector, clock, START)
    assert condor is None and reason == "incomplete_chain"


# --- Scenario: probe-match integrity invariant (STK-11, v1.40 wording) -------

@given('the probe walk selects a strike')
def _(world):
    side = ChainSide((D("6000"), D("5950")),
                     {D("6000"): _mk("2.93"), D("5950"): _mk("0.10")})
    world["selected"] = select_side(side, **WALK)
    assert isinstance(world["selected"], Selected)


@then('its raw mid is within 0.025 of the matched probe price')
def _(world):
    r = world["selected"]
    assert abs(r.short_mid - r.probe_price) <= D("0.025")


@then('the day report records the matched probe number')
def _(world):
    r = world["selected"]
    assert isinstance(r.probe_number, int) and r.probe_number >= 1  # the log's source field


# --- Scenario: missing wing retries ------------------------------------------

@given('the wing strike has no mark at fire time but appears at T+15s')
def _(world):
    full = _healthy_snapshot()
    # Only the WING is missing — everything else in the (large) reachable set
    # stays marked, so completeness_ok still passes: this scenario exercises
    # the WingUnmarked retry path, not the completeness gate.
    t0_put = ChainSide(full.put_side.strikes_toward_otm,
                       {k: v for k, v in full.put_side.marks.items() if k != WING_STRIKE})
    reachable = _reachable(full.put_side, D(-1))
    assert completeness_ok(t0_put, reachable=reachable, completeness_pct=CFG.completeness_pct)
    world["full"] = full
    world["t0_put"] = t0_put
    world["heal_after_s"] = 15


@then('the entry proceeds with the correct wing (no guessing, no immediate skip)')
def _(world):
    clock = FakeClock(START)
    full, t0_put, heal_after = world["full"], world["t0_put"], world["heal_after_s"]

    async def provider():
        elapsed = (clock.now() - START).total_seconds()
        put_side = full.put_side if elapsed >= heal_after else t0_put
        return Snap(put_side, full.call_side)

    selector = LiveCondorSelector(snapshot_provider=provider, config=CFG, clock=clock,
                                  entry_window_seconds=120, chain_retry_seconds=5)
    condor, reason = _drive_to_completion(selector, clock, START)
    assert condor is not None and reason is None      # no immediate skip
    assert condor.put_long == WING_STRIKE              # the correct wing, not a guess


# --- Scenario: far-OTM emptiness ---------------------------------------------

@given('strikes outside the ATM band have no bids')
def _(world):
    full = _healthy_snapshot()
    far = (D("5000"), D("4900"))   # nowhere near the reachable set; never marked
    extended = ChainSide(full.put_side.strikes_toward_otm + far, full.put_side.marks)
    reachable = _reachable(extended, D(-1))
    world["far"] = far
    world["reachable"] = reachable
    world["far_otm_ok"] = completeness_ok(extended, reachable=reachable,
                                          completeness_pct=CFG.completeness_pct)


@then('the chain-integrity gate still passes')
def _(world):
    assert world["far_otm_ok"] is True
    assert not (set(world["far"]) & world["reachable"])   # far strikes never join the set


# --- Scenario: far-OTM dead strikes never block (v1.51 regression) ----------

@given("every strike in the entry's reachable set has fresh two-sided marks")
def _(world):
    world["full"] = _healthy_snapshot()


@given('calls 55+ points OTM outside the reachable set are listed but never quoted')
def _(world):
    full = world["full"]
    reachable = _reachable(full.call_side, D(1))
    assert max(reachable) == D("6115")   # the actual reachable ceiling for this curve
    far = tuple(SPOT + D(p) for p in (175, 180, 185, 190))   # 60+ pts beyond it, unquoted
    extended_call = ChainSide(full.call_side.strikes_toward_otm + far, full.call_side.marks)
    world["snap"] = Snap(full.put_side, extended_call)
    world["reachable_call"] = reachable
    world["far_calls"] = far


@then('the STK-10 gate PASSES and selection proceeds')
def _(world):
    assert not (set(world["far_calls"]) & world["reachable_call"])

    async def provider():
        return world["snap"]

    selector = LiveCondorSelector(snapshot_provider=provider, config=CFG)   # no clock: single attempt suffices
    condor, reason = asyncio.run(selector(START, 1))
    assert condor is not None and reason is None


# --- Scenario: a dead long wing is caught upfront ----------------------------

@given('the reachable set includes the wing strike and its quote is missing')
def _(world):
    # A minimal, deliberately small reachable set (no shift budgets) so a
    # single missing strike (the wing) is enough to fail completeness_pct:
    # short (marked) + its wing (unmarked) = 1/2 = 50%.
    strikes = (D("6000"), D("5995"), D("5945"))   # 5995's wing at width 50 is 5945
    side = ChainSide(strikes, {D("6000"): _mk("3.60"), D("5995"): _mk("3.00")})  # 5945 UNMARKED
    reachable = reachable_strikes(side, target_premium=D("3.00"), wing_width=D("50"),
                                  otm_direction=D(-1), min_short_premium=D("1.00"),
                                  max_strike_shifts=0, max_long_shifts=0)
    assert reachable == frozenset({D("5995"), D("5945")})
    world["side"] = side
    world["reachable"] = reachable


@then('the gate counts it against completeness (no later wing_unmarked surprise)')
def _(world):
    assert not completeness_ok(world["side"], reachable=world["reachable"],
                               completeness_pct=D("90"))


# --- Scenario: chain_atm_band_pts is retired ---------------------------------

@given('config contains chain_atm_band_pts')
def _(world):
    world["cfg"] = {"chain_atm_band_pts": 150}


@then('config validation rejects it as an unknown retired key')
def _(world):
    try:
        validate_config(world["cfg"])
        raise AssertionError("chain_atm_band_pts should have been rejected")
    except ConfigRejected as e:
        assert e.key == "chain_atm_band_pts" and e.reason == "removed_v151"
