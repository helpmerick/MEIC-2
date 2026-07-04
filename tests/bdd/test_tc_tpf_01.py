"""Hand-written step definitions for TC-TPF-01 — TPF level-selector math (Phase 3).

Each scenario's profit context lives in its name (25% / 75% / 23%); the Then
steps are self-contained assertions against the shared domain math in
meic.domain.tpf — the same functions the UI selector and the backend
validator both call (two-layer validation, v1.6).
"""
from decimal import Decimal as D

from pytest_bdd import scenarios, then

from meic.domain.tpf import ALL_LEVELS, is_armable, valid_levels

scenarios("../features/TC-TPF-01.feature")


@then('enabled levels are exactly {5, 10, 15, 20}')
def _():
    assert valid_levels(D("25")) == (5, 10, 15, 20)


@then('25 and above are disabled with reason "too close - would trigger immediately"')
def _():
    assert all(not is_armable(level, D("25")) for level in ALL_LEVELS if level >= 25)


@then('enabled levels are exactly {5, 10, ..., 70}')
def _():
    assert valid_levels(D("75")) == tuple(range(5, 75, 5))


@then('75 and above are disabled')
def _():
    assert all(not is_armable(level, D("75")) for level in ALL_LEVELS if level >= 75)


@then('the highest enabled level is 15   # 20 violates the 5-point gap (23 - 20 < 5)')
def _():
    assert max(valid_levels(D("23"))) == 15
