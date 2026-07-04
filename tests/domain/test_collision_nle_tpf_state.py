"""Domain unit tests: STK-09 collisions, NLE-01 math, TPF math, doc 05 §3 table."""
from decimal import Decimal as D

import pytest

from meic.domain.collision import Abort, Resolved, resolve_collisions
from meic.domain.nle import EstimateUnavailable, NetLossEstimate, estimate_net_loss
from meic.domain.side_state import SideState, assert_transition, can_transition
from meic.domain.tpf import ALL_LEVELS, floor_amount, is_armable, valid_levels

# Put-side listed ladder, 5-point steps, nearest-money first (TC-STK-06 numbers)
LADDER = tuple(D(str(s)) for s in range(5990, 5900, -5))


def occ(**by_strike):
    return {D(k): frozenset(v) for k, v in by_strike.items()}


def resolve(**over):
    base = dict(
        short_strike=D("5990"), long_strike=D("5940"),
        occupancy={}, listed_strikes_toward_otm=LADDER,
        wing_width=D("50"), otm_direction=D(-1),
    )
    base.update(over)
    return resolve_collisions(**base)


class TestCollision:
    def test_long_at_short_target_forces_one_shift_wing_follows(self):
        r = resolve(occupancy={D("5990"): frozenset({"long"})})
        assert r == Resolved(D("5985"), D("5935"), short_shifts=1, long_shifts=0)

    def test_three_blocked_strikes_abort(self):
        r = resolve(occupancy=occ(**{"5990": {"long"}, "5985": {"long"}, "5980": {"long"}}))
        assert r == Abort("strike_collision")

    def test_same_type_stacks_short_on_short(self):
        r = resolve(occupancy={D("5990"): frozenset({"short"})})
        assert isinstance(r, Resolved) and r.short_strike == D("5990") and r.short_shifts == 0

    def test_same_type_stacks_long_on_long(self):
        r = resolve(occupancy={D("5940"): frozenset({"long"})})
        assert isinstance(r, Resolved) and r.long_strike == D("5940") and r.long_shifts == 0

    def test_long_shifts_alone_when_wing_holds_short_spread_widens(self):
        r = resolve(occupancy={D("5940"): frozenset({"short"})})
        assert r == Resolved(D("5990"), D("5935"), short_shifts=0, long_shifts=1)
        assert r.widened  # RSK-04 must re-gate the widened worst case

    def test_five_failed_long_shifts_abort(self):
        blocked = {str(s): {"short"} for s in range(5940, 5910, -5)}  # 5940..5915: 6 strikes
        r = resolve(occupancy=occ(**blocked))
        assert r == Abort("strike_collision")

    def test_shift_off_listed_ladder_aborts(self):
        r = resolve(short_strike=D("5910"), long_strike=D("5860"),
                    occupancy={D("5910"): frozenset({"long"}), D("5905"): frozenset({"long"})})
        assert r == Abort("strike_collision")


class TestNLE:
    # TC-NLE-01 scripted chain, verbatim
    CHAIN = {D("5990"): D("1.35"), D("5960"): D("3.10"), D("5950"): D("4.20"),
             D("5945"): D("5.14"), D("5940"): D("0.15"), D("5985"): D("1.55")}

    def test_tc_nle_01_hand_computation(self):
        r = estimate_net_loss(
            chain_mids=self.CHAIN,
            short_strike=D("5990"), short_fill=D("1.35"),
            long_strike=D("5940"), long_fill=D("0.15"),
            stop_trigger=D("5.14"), nle_haircut_pct=D("30"),
        )
        assert isinstance(r, NetLossEstimate)
        assert r.implied_move == D("45")
        assert r.raw_long_estimate == D("1.55")
        assert r.haircut_estimate == D("1.085")
        assert r.estimated_net_loss == D("2.855")

    def test_nle_03_too_few_strikes_is_unavailable_not_error(self):
        r = estimate_net_loss(
            chain_mids={D("5990"): D("1.35")},
            short_strike=D("5990"), short_fill=D("1.35"),
            long_strike=D("5940"), long_fill=D("0.15"),
            stop_trigger=D("5.14"), nle_haircut_pct=D("30"),
        )
        assert isinstance(r, EstimateUnavailable)

    def test_trigger_outside_range_is_unavailable(self):
        r = estimate_net_loss(
            chain_mids={D("5990"): D("1.35"), D("5985"): D("1.55")},
            short_strike=D("5990"), short_fill=D("1.35"),
            long_strike=D("5940"), long_fill=D("0.15"),
            stop_trigger=D("99"), nle_haircut_pct=D("30"),
        )
        assert isinstance(r, EstimateUnavailable)


class TestTPF:
    def test_levels_are_5_to_90_step_5(self):
        assert ALL_LEVELS == tuple(range(5, 95, 5)) and 90 in ALL_LEVELS and 95 not in ALL_LEVELS

    def test_selectable_only_5_points_below_current(self):
        assert valid_levels(D("47.3")) == tuple(range(5, 45, 5))  # up to 40
        assert valid_levels(D("10")) == (5,)
        assert valid_levels(D("9.9")) == ()

    def test_backend_rejects_never_clamps(self):
        assert is_armable(40, D("47.3"))
        assert not is_armable(45, D("47.3"))
        assert not is_armable(42, D("99"))  # not in the discrete set

    def test_floor_amount_pct_of_net_credit(self):
        assert floor_amount(30, D("2.30")) == D("0.69")
        with pytest.raises(ValueError):
            floor_amount(42, D("2.30"))


class TestSideStateTable:
    def test_happy_path_chain_is_legal(self):
        path = [SideState.PENDING, SideState.WORKING, SideState.OPEN_UNCONFIRMED_STOP,
                SideState.PROTECTED, SideState.SIDE_STOPPED, SideState.LONG_LIQUIDATING,
                SideState.SIDE_CLOSED]
        for src, dst in zip(path, path[1:]):
            assert_transition(src, dst)

    def test_unlisted_transitions_are_bugs(self):
        assert not can_transition(SideState.PENDING, SideState.PROTECTED)
        assert not can_transition(SideState.SKIPPED, SideState.WORKING)
        assert not can_transition(SideState.SIDE_CLOSED, SideState.PROTECTED)
        with pytest.raises(ValueError):
            assert_transition(SideState.PENDING, SideState.PROTECTED)

    def test_manual_and_suspended_reachable_from_any_state(self):
        for s in SideState:
            assert can_transition(s, SideState.MANUAL)      # UC-08
            assert can_transition(s, SideState.SUSPENDED)   # OWN-06/09/10/11

    def test_decay_reinflation_guard_returns_to_protected(self):
        assert can_transition(SideState.DECAY_CLOSING, SideState.PROTECTED)
        assert can_transition(SideState.LONG_LIQUIDATING, SideState.SIDE_EXPIRED)  # EOD-04
