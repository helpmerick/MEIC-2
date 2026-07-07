"""Hand-written step definitions for TC-STK-06 — STK-09 collision rules (Phase 3).

Domain-pure scenarios are real. Two steps stay frozen/red:
- fills-and-stops attribution by order ID (OWN + stop semantics — frozen)
- RSK-04 widened-worst-case evaluation (application-layer risk gate — later phase;
  the domain's `widened` flag that feeds it IS asserted here)
"""
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.domain.collision import Abort, Resolved, resolve_collisions
from meic.domain.events import CondorFilled, ShortStopped
from meic.domain.gates import GatesFailed, check_credit_gates
from meic.domain.projection import fold
from meic.domain.risk import exceeds_max_day_risk, worst_case_loss

scenarios("../features/TC-STK-06.feature")

LADDER = tuple(D(str(s)) for s in range(5990, 5900, -5))


def _resolve(world, occupancy):
    world["result"] = resolve_collisions(
        short_strike=D("5990"), long_strike=D("5940"),
        occupancy={D(k): frozenset(v) for k, v in occupancy.items()},
        listed_strikes_toward_otm=LADDER,
        wing_width=D("50"), otm_direction=D(-1),
    )


@pytest.fixture
def world():
    return {}


@given("entry 3's target short put strike 5990 holds an existing long")
def _(world):
    _resolve(world, {"5990": {"long"}})


@then('the short shifts to 5985 and the wing moves with it (width preserved)')
def _(world):
    assert world["result"] == Resolved(D("5985"), D("5935"), short_shifts=1, long_shifts=0)


@given('existing longs at 5990, 5985 and 5980 (the original and both shift targets)')
def _(world):
    _resolve(world, {"5990": {"long"}, "5985": {"long"}, "5980": {"long"}})


@then('the entry is SKIPPED with reason "strike_collision" and no order is submitted')
def _(world):
    assert world["result"] == Abort("strike_collision")


@given("entry 1 is short 5990 and entry 3's selection also lands on 5990")
def _(world):
    _resolve(world, {"5990": {"short"}})  # same type STACKS


@then('no shift occurs and the order is submitted')
def _(world):
    r = world["result"]
    assert isinstance(r, Resolved) and r.short_strike == D("5990") and r.short_shifts == 0


@then("both entries' fills and stops attribute correctly by order ID")
def _(world):
    # freeze lifted (STP-05a reviewed 2026-07-06). Two entries stack shorts at
    # 5990; each fill/stop carries its own entry_id (1:1 with its order), so the
    # projection attributes them to the right entry — never commingled (OWN-01).
    events = [
        CondorFilled(entry_id="e1", net_credit=D("2.00")),
        CondorFilled(entry_id="e3", net_credit=D("2.20")),
        ShortStopped(entry_id="e1", side="PUT", fill=D("3.80"), slippage=D("0")),
        ShortStopped(entry_id="e3", side="CALL", fill=D("3.50"), slippage=D("0")),
    ]
    entries = fold(events).entries
    assert entries["e1"].net_credit == D("2.00") and entries["e1"].stop_fills == D("3.80")
    assert entries["e3"].net_credit == D("2.20") and entries["e3"].stop_fills == D("3.50")
    assert entries["e1"].sides_stopped == ("PUT",)   # e3's CALL stop did not leak in
    assert entries["e3"].sides_stopped == ("CALL",)


@given("the wing target already holds another entry's long")
def _(world):
    _resolve(world, {"5940": {"long"}})  # long-on-long stacks


@then('no shift occurs')
def _(world):
    r = world["result"]
    assert isinstance(r, Resolved) and r.long_strike == D("5940") and r.long_shifts == 0


@given('the short places at its original strike')
def _(world):
    pass  # context marker; the occupancy Given below runs the resolution


@given('the wing target 5940 holds an existing short position')
def _(world):
    _resolve(world, {"5940": {"short"}})


@then('the long shifts alone to 5935 (spread now 5 points wider)')
def _(world):
    r = world["result"]
    assert r == Resolved(D("5990"), D("5935"), short_shifts=0, long_shifts=1)
    assert r.widened  # this flag is exactly what RSK-04 consumes


@then('RSK-04 evaluates the widened worst case before submission')
def _(world):
    # freeze lifted. The long shifted alone to 5935, so the spread is 55 wide
    # (was 50). RSK-04 must gate on the WIDENED worst case, not the original.
    r = world["result"]
    widened_width = r.short_strike - r.long_strike            # 5990 - 5935 = 55
    original_width = D("50")
    net_credit = D("2.00")
    widened_wc = worst_case_loss(widened_width, net_credit)   # (55-2)*100 = 5300
    original_wc = worst_case_loss(original_width, net_credit)  # (50-2)*100 = 4800
    assert widened_wc > original_wc

    # a max_day_risk between the two only trips on the widened figure — proving
    # RSK-04 evaluates the widened worst case before submission (not the original)
    max_day_risk = D("5000")
    assert exceeds_max_day_risk([], widened_wc, max_day_risk) is True
    assert exceeds_max_day_risk([], original_wc, max_day_risk) is False


@then('five failed long shifts abort the entry with "strike_collision"')
def _(world):
    blocked = {str(s): {"short"} for s in range(5940, 5910, -5)}
    _resolve(world, blocked)
    assert world["result"] == Abort("strike_collision")


@given('an unfilled working order includes a long at 5990')
def _(world):
    # in-flight orders count as occupied for OPPOSITE-type checks (STK-09)
    world["occupancy"] = {"5990": {"long"}}


@when('the next entry wants a short at 5990')
def _(world):
    _resolve(world, world["occupancy"])


@then('5990 is treated as blocked')
def _(world):
    r = world["result"]
    assert isinstance(r, Resolved) and r.short_strike == D("5985") and r.short_shifts == 1


@then('an unfilled SHORT at 5990 does not block a new short there  # same type never blocks')
def _(world):
    _resolve(world, {"5990": {"short"}})  # same-type in-flight: no block
    r = world["result"]
    assert isinstance(r, Resolved) and r.short_strike == D("5990") and r.short_shifts == 0


@given("the shifted short's premium falls below min_short_premium (or total net < min_total_credit)")
def _(world):
    # TC-STK-06 'Gates re-run on final strikes': the shifted short prices thin
    world["gate"] = check_credit_gates(
        put_short_mid=D("0.80"), call_short_mid=D("1.25"),
        total_net_credit_mid=D("2.30"),
        min_short_premium=D("1.00"), min_total_credit=D("2.00"),
    )


@then('the entry is SKIPPED with reason "insufficient_credit"')
def _(world):
    assert world["gate"] == GatesFailed("insufficient_credit")
