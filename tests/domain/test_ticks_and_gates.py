"""Domain unit tests: STK-08 tick rounding, STK-05/06 credit gates."""
from decimal import Decimal as D

import pytest

from meic.domain.gates import GatesFailed, GatesPassed, check_credit_gates
from meic.domain.ticks import TickRung, TickTable

# SPX's documented structure as FIXTURE DATA (production obtains it from the
# API — STK-08 forbids hardcoding it in domain logic).
SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


class TestTickRounding:
    @pytest.mark.parametrize("raw, expected", [
        ("2.6325", "2.65"),  # 1.35 * 1.95 — TC-STP-01 arithmetic, sub-$3 tick
        ("2.4375", "2.45"),  # 1.25 * 1.95
        ("2.185", "2.20"),   # 0.95 * 2.30
        ("2.99", "3.00"),    # top of the 0.05 rung
        ("3.05", "3.10"),    # 0.10 rung, half rounds up
        ("3.00", "3.00"),    # boundary belongs to the 0.10 rung
        ("5.1375", "5.10"),  # 0.10 rung, below half rounds down
    ])
    def test_rounds_to_injected_table(self, raw, expected):
        assert SPX.round(D(raw)) == D(expected)

    def test_tick_selection_per_rung(self):
        assert SPX.tick_for(D("2.99")) == D("0.05")
        assert SPX.tick_for(D("3.00")) == D("0.10")  # $0.10 at/above

    def test_table_requires_catch_all(self):
        with pytest.raises(ValueError):
            TickTable((TickRung(D("3.00"), D("0.05")),))


class TestCreditGates:
    def kw(self, **over):
        base = dict(
            put_short_mid=D("1.35"), call_short_mid=D("1.25"),
            total_net_credit_mid=D("2.30"),
            min_short_premium=D("1.00"), min_total_credit=D("2.00"),
        )
        base.update(over)
        return base

    def test_healthy_entry_passes(self):
        assert isinstance(check_credit_gates(**self.kw()), GatesPassed)

    def test_stk05_put_short_below_gross_floor_skips_whole_entry(self):
        # TC-STK-03: short put mid 0.80 vs floor 1.00 — single-side prohibited
        r = check_credit_gates(**self.kw(put_short_mid=D("0.80")))
        assert r == GatesFailed("insufficient_credit")

    def test_stk06_expensive_wings_kill_the_net(self):
        # TC-STK-02: 12:30 wings cost 2.10 each => total net 1.90 < 2.00
        r = check_credit_gates(**self.kw(total_net_credit_mid=D("1.90")))
        assert r == GatesFailed("insufficient_credit")

    def test_thin_side_trades_when_total_passes(self):
        # TC-STK-02 accepted-by-design: put nets 0.10, call 2.20, total 2.30
        r = check_credit_gates(**self.kw(total_net_credit_mid=D("2.30")))
        assert isinstance(r, GatesPassed) and r.total_net_credit == D("2.30")
