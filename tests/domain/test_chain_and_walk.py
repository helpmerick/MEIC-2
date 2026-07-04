"""Domain unit tests: STK-10/11 chain guards, STK-02/03/07 premium walk.

Put-side fixtures: strikes_toward_otm ordered DESCENDING (nearest money first).
"""
from decimal import Decimal as D

from meic.domain.chain import ChainSide, Mark, adjacency_ok, completeness_ok
from meic.domain.walk import Selected, Skip, WingUnmarked, select_side


def put_side(marks: dict, strikes=None) -> ChainSide:
    strikes = strikes or sorted(marks, reverse=True)
    return ChainSide(tuple(D(str(s)) for s in strikes), {D(str(k)): v for k, v in marks.items()})


def mk(mid, bid=None):  # a Mark with the given mid (symmetric spread)
    mid = D(str(mid))
    bid = D(str(bid)) if bid is not None else mid - D("0.05")
    return Mark(bid=bid, ask=2 * mid - bid)


WALK = dict(target_premium=D("3.00"), tolerance=D("0.10"), wing_width=D("50"), otm_direction=D(-1))


class TestPremiumWalk:
    def test_tc_stk_02_overshoot_within_tolerance_accepted(self):
        # mids 3.10 and 2.85 => the 3.10 strike wins (ceiling inclusive)
        side = put_side({6000: mk("3.10"), 5995: mk("2.85"), 5950: mk("0.60"), 5945: mk("0.55")},
                        strikes=[6000, 5995, 5950, 5945])
        r = select_side(side, **WALK)
        assert isinstance(r, Selected) and r.short_strike == D("6000") and r.long_strike == D("5950")

    def test_tc_stk_02_one_tick_over_ceiling_rejected(self):
        # 3.11 > 3.10 ceiling => the 2.85 strike is selected
        side = put_side({6000: mk("3.11"), 5995: mk("2.85"), 5945: mk("0.55")},
                        strikes=[6000, 5995, 5945])
        r = select_side(side, **WALK)
        assert isinstance(r, Selected) and r.short_strike == D("5995")

    def test_tc_stk_02_ceiling_beats_proximity(self):
        side = put_side({6000: mk("3.25"), 5995: mk("2.85"), 5945: mk("0.55")},
                        strikes=[6000, 5995, 5945])
        r = select_side(side, **WALK)
        assert isinstance(r, Selected) and r.short_strike == D("5995")

    def test_tc_stk_02_nothing_at_or_below_ceiling(self):
        side = put_side({6000: mk("4.00"), 5995: mk("3.50")}, strikes=[6000, 5995])
        assert select_side(side, **WALK) == Skip("no_valid_strikes")

    def test_stk_07_zero_bid_short_skips(self):
        side = put_side({6000: mk("3.05", bid="0"), 5950: mk("0.60")}, strikes=[6000, 5950])
        assert select_side(side, **WALK) == Skip("no_valid_strikes")

    def test_stk_11_hole_one_step_closer_rejects(self):
        # 6000 unmarked (hole), 5995 marked at 2.85: adjacency can't prove continuity
        side = put_side({5995: mk("2.85"), 5945: mk("0.55")}, strikes=[6000, 5995, 5945])
        assert select_side(side, **WALK) == Skip("incomplete_chain")

    def test_stk_11_leapt_hole_rejects(self):
        # closer strike marked BELOW the ceiling => walk should have taken it —
        # but it was skipped as a data error elsewhere: guard flags the leap
        side = put_side({6000: mk("3.02"), 5995: mk("2.85"), 5945: mk("0.55")},
                        strikes=[6000, 5995, 5945])
        # force selection to land on 5995 by unmarking 6000's bid? No — walk takes
        # 6000 (<= ceiling). Instead: 6000 marked above ceiling, 5995 hole, 5990 marked.
        side = put_side({6000: mk("3.25"), 5990: mk("2.85"), 5940: mk("0.55")},
                        strikes=[6000, 5995, 5990, 5940])
        assert select_side(side, **WALK) == Skip("incomplete_chain")  # 5995 is a hole

    def test_wing_unmarked_is_retry_not_skip(self):
        side = put_side({6000: mk("3.05"), 5995: mk("2.85")}, strikes=[6000, 5995, 5950])
        r = select_side(side, **WALK)
        assert isinstance(r, WingUnmarked) and r.long_strike == D("5950")

    def test_unlisted_wing_skips(self):
        side = put_side({6000: mk("3.05")}, strikes=[6000, 5995])
        assert select_side(side, **WALK) == Skip("no_valid_strikes")


class TestChainGuards:
    def test_stk_10_completeness_below_pct_fails(self):
        # TC-STK-07: 75% marked at fire time vs 90% requirement
        band = tuple(D(str(s)) for s in [6000, 5995, 5990, 5985])
        side = put_side({6000: mk("3.0"), 5995: mk("2.8"), 5990: mk("2.6")}, strikes=list(band))
        assert not completeness_ok(side, band_strikes=band, completeness_pct=D("90"))
        assert completeness_ok(side, band_strikes=band, completeness_pct=D("75"))

    def test_stk_10_far_otm_emptiness_never_trips(self):
        # strikes outside the band are not in band_strikes => irrelevant
        band = tuple(D(str(s)) for s in [6000, 5995])
        side = put_side({6000: mk("3.0"), 5995: mk("2.8")}, strikes=[6000, 5995, 5000, 4900])
        assert completeness_ok(side, band_strikes=band, completeness_pct=D("90"))

    def test_stk_11_nearest_money_strike_passes_vacuously(self):
        side = put_side({6000: mk("3.0")}, strikes=[6000])
        assert adjacency_ok(side, D("6000"), ceiling=D("3.10"))
