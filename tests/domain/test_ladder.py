"""Domain unit tests: ORD-02/03 + LEX-03/04 ladder mechanics."""
from decimal import Decimal as D

from meic.domain.ladder import RepriceLadder, intrinsic_call, intrinsic_put, lex_floor
from meic.domain.ticks import TickRung, TickTable

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


class TestRepriceLadder:
    def test_entry_ladder_ord02_defaults(self):
        # mid 2.50, 5 reprices, floor 2.00: full sequence survives
        ladder = RepriceLadder(start=D("2.50"), ticks=SPX, attempts=5, floor=D("2.00"))
        assert [s.price for s in ladder.prices()] == [D(p) for p in ("2.50", "2.45", "2.40", "2.35", "2.30", "2.25")]

    def test_ord03_floor_cuts_the_sequence(self):
        ladder = RepriceLadder(start=D("2.10"), ticks=SPX, attempts=5, floor=D("2.00"))
        assert [s.price for s in ladder.prices()] == [D("2.10"), D("2.05"), D("2.00")]

    def test_start_below_floor_is_empty(self):
        assert RepriceLadder(start=D("1.90"), ticks=SPX, attempts=5, floor=D("2.00")).prices() == ()

    def test_tick_boundary_crossing_uses_current_rung(self):
        ladder = RepriceLadder(start=D("3.10"), ticks=SPX, attempts=2, floor=None)
        assert [s.price for s in ladder.prices()] == [D("3.10"), D("3.00"), D("2.90")]

    def test_unrounded_start_is_tick_rounded_first(self):
        ladder = RepriceLadder(start=D("2.5325"), ticks=SPX, attempts=1, floor=None)
        assert ladder.prices()[0].price == D("2.55")


class TestLexFloor:
    def test_lex04_intrinsics(self):
        assert intrinsic_put(D("5990"), D("5950")) == D("40")
        assert intrinsic_put(D("5990"), D("6000")) == D("0")
        assert intrinsic_call(D("5990"), D("6050")) == D("60")
        assert intrinsic_call(D("5990"), D("5950")) == D("0")

    def test_lex04_floor_is_max_of_bid_and_intrinsic(self):
        assert lex_floor(D("0.55"), D("40")) == D("40")
        assert lex_floor(D("41.20"), D("40")) == D("41.20")
