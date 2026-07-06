"""Domain unit tests: STK-10 chain gate, STK-02 probe walk (v1.39).

The eight TC-STK-08 vectors are pinned HERE at unit level because the
extracted TC-STK-08.feature is currently invalid Gherkin (wrapped step line,
amendment proposed to the operator) — the BDD binding follows once ratified.

Put-side fixtures: strikes_toward_otm ordered DESCENDING (nearest money first).
"""
from decimal import Decimal as D

import pytest

from meic.domain.chain import ChainSide, Mark, completeness_ok
from meic.domain.walk import Selected, Skip, WingUnmarked, lattice_price, probe_prices, select_side


def put_side(mids: dict, strikes=None, bids: dict | None = None) -> ChainSide:
    """mids: strike -> raw mid. Wing strikes included automatically 50 below
    each candidate unless the test provides its own strike list."""
    strikes = strikes or sorted({*mids, *(D(str(k)) - 50 for k in map(D, map(str, mids)))}, reverse=True)
    marks = {}
    for k, mid in mids.items():
        m = D(str(mid))
        bid = D(str((bids or {}).get(k))) if bids and k in bids else m - D("0.02")
        marks[D(str(k))] = Mark(bid=bid, ask=2 * m - bid)
    return ChainSide(tuple(D(str(s)) for s in strikes), marks)


WALK = dict(target_premium=D("3.00"), wing_width=D("50"), otm_direction=D(-1))


def wings(*shorts):
    """Cheap marked wings 50 points below the given short strikes."""
    return {s - 50: "0.10" for s in shorts}


class TestProbeSequence:
    def test_exact_deterministic_order(self):
        seq = probe_prices(D("3.00"), floor=D("1.75"))
        head = tuple(D(p) for p in ("3.00", "2.95", "3.05", "2.90", "3.10", "2.85", "3.15", "2.80", "2.75"))
        assert seq[: len(head)] == head
        assert seq[-1] == D("1.75")  # floor probe inclusive
        assert len(seq) == 1 + 25 + 3  # T + down + up

    def test_floor_truncates_down_probes(self):
        seq = probe_prices(D("2.00"), floor=D("1.00"))
        assert min(seq) == D("1.00") and D("0.95") not in seq

    def test_lattice_rounding(self):
        assert lattice_price(D("2.93")) == D("2.95")
        assert lattice_price(D("2.92")) == D("2.90")
        assert lattice_price(D("3.20")) == D("3.20")


class TestProbeWalkVectors:
    """TC-STK-08 vectors A–E2 + lattice, pinned at unit level."""

    def test_vector_a_first_down_probe_matches(self):
        side = put_side({6000: "3.20", 5995: "2.93", 5990: "2.70", **wings(5995)})
        r = select_side(side, **WALK)
        assert isinstance(r, Selected)
        assert r.short_strike == D("5995") and r.probe_price == D("2.95") and r.probe_number == 2

    def test_vector_b_up_probe_within_cap_matches(self):
        side = put_side({6000: "3.30", 5995: "3.05", 5990: "2.80", **wings(5995)})
        r = select_side(side, **WALK)
        assert isinstance(r, Selected)
        assert r.short_strike == D("5995") and r.probe_price == D("3.05") and r.probe_number == 3

    def test_vector_c_equal_distance_above_cap_never_selected(self):
        side = put_side({6000: "3.45", 5995: "3.20", 5990: "2.80", **wings(5990)})
        r = select_side(side, **WALK)
        assert isinstance(r, Selected)
        assert r.short_strike == D("5990") and r.probe_price == D("2.80")  # 3.20 skipped forever

    def test_vector_d_full_exhaustion_skips(self):
        side = put_side({6000: "3.45", 5995: "1.60", **wings(6000, 5995)})  # nothing in [1.75, 3.15]
        assert select_side(side, **WALK) == Skip("no_valid_strikes")

    def test_vector_e_deep_walk_sells_thin_but_legal(self):
        side = put_side({5990: "1.80", **wings(5990)})
        r = select_side(side, **WALK)
        assert isinstance(r, Selected) and r.short_strike == D("5990") and r.probe_price == D("1.80")

    def test_vector_e2_hard_floor_beats_walk_depth(self):
        side = put_side({5990: "0.95", **wings(5990)})
        r = select_side(side, target_premium=D("2.00"), wing_width=D("50"), otm_direction=D(-1))
        assert r == Skip("no_valid_strikes")  # floor = max(0.75, 1.00) = 1.00

    def test_lattice_answers_290_not_295(self):
        side = put_side({5995: "2.92", **wings(5995)})
        r = select_side(side, **WALK)
        assert isinstance(r, Selected) and r.probe_price == D("2.90") and r.probe_number == 4


class TestProbeWalkMechanics:
    def test_tie_same_probe_price_closest_raw_mid_wins(self):
        # both round to 2.95: 2.94 (dist .01) beats 2.97 (dist .02)
        side = put_side({6000: "2.97", 5995: "2.94", **wings(6000, 5995)})
        r = select_side(side, **WALK)
        assert isinstance(r, Selected) and r.short_strike == D("5995")

    def test_tie_equal_distance_goes_further_otm(self):
        # 2.94 and 2.96 both dist .01 from 2.95: further OTM (5995) wins
        side = put_side({6000: "2.96", 5995: "2.94", **wings(6000, 5995)})
        r = select_side(side, **WALK)
        assert isinstance(r, Selected) and r.short_strike == D("5995")

    def test_stk07_zero_bid_short_skips(self):
        side = put_side({6000: "3.00", **wings(6000)}, bids={6000: "0"})
        assert select_side(side, **WALK) == Skip("no_valid_strikes")

    def test_wing_unmarked_is_retry_not_skip(self):
        side = put_side({6000: "3.00"}, strikes=[6000, 5995, 5950])
        r = select_side(side, **WALK)
        assert isinstance(r, WingUnmarked) and r.long_strike == D("5950")

    def test_unlisted_wing_skips(self):
        side = put_side({6000: "3.00"}, strikes=[6000, 5995])
        assert select_side(side, **WALK) == Skip("no_valid_strikes")


class TestChainGuards:
    def test_stk_10_completeness_below_pct_fails(self):
        band = tuple(D(str(s)) for s in [6000, 5995, 5990, 5985])
        side = put_side({6000: "3.0", 5995: "2.8", 5990: "2.6"}, strikes=list(band))
        assert not completeness_ok(side, band_strikes=band, completeness_pct=D("90"))
        assert completeness_ok(side, band_strikes=band, completeness_pct=D("75"))

    def test_stk_10_far_otm_emptiness_never_trips(self):
        band = tuple(D(str(s)) for s in [6000, 5995])
        side = put_side({6000: "3.0", 5995: "2.8"}, strikes=[6000, 5995, 5000, 4900])
        assert completeness_ok(side, band_strikes=band, completeness_pct=D("90"))
