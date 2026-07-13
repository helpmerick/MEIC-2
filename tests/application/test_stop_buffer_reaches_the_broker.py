"""STP-02b `stop_rebate_markup` — pinning the ROW -> BROKER wiring end to end.

Why this file exists: STP-02b (the operator's long-recovery buffer) is added
to the stop trigger inside `domain/stop_policy.stop_trigger` — that pure
function is already pinned by TC-STP-14 (tests/bdd/test_tc_stp_14.py). And
`ProtectPosition.protect()` is already pinned by test_protect_position.py's
`test_stop_placed_journals_the_markup_in_force` — but THAT test passes
`markup=` into `protect()` BY HAND.

Neither test drives a SCHEDULE ROW's `stop_rebate_markup` through the real
production wiring that is supposed to carry it: `ScheduledRow.stop` (or
manual_entry's `_stop(row)`) -> `ExecuteEntryAttempt.attempt(stop=...)` ->
`composition/live.py::_on_filled` -> `ProtectPosition.protect(markup=...)` ->
the broker-resting stop order + the journaled `StopPlaced` event. Every link
in that chain was traced and proven correct by hand (2026-07-13), but nothing
EXERCISES it: a regression that silently dropped the row's markup to 0.00
anywhere along that chain (e.g. someone "simplifying" `_on_filled` to read
`self.execute.default_stop.markup` unconditionally) would leave the whole
suite green while the operator's $0.30 buffer arrived at the broker as $0.00.

These tests drive the REAL `LiveComposition` (composition/live.py, completely
unmodified) with only its network-facing broker swapped for the offline
`FakeBroker` harness — `_on_filled`, `_shorts` (the LegBook lookup),
`ProtectPosition`, `ExecuteEntryAttempt`, `ScheduledRow.stop`, and
`ManualEntry`/`manual_entry._stop` all run as production code, unaltered.

Operator-requested guarantee, 2026-07-13, ahead of a live trade with a $0.30
`stop_rebate_markup`.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import date, datetime, time, timezone
from decimal import Decimal as D

from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor
from meic.application.manual_entry import ManualEntry
from meic.composition.live import LiveComposition
from meic.composition.live_runtime import LiveRuntime, ScheduledRow
from meic.domain.events import StopPlaced
from meic.domain.schedule import ResolvedEntry
from meic.domain.stop_policy import StopBasis, stop_trigger
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import FastClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
WHEN = datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc)
IS_CONDOR = lambda o: o.kind == "iron_condor"
CREDIT = D("5.20")   # the operator's real live vector (2026-07-10 journal shape)


def _jwt(iss: str) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


def _cert_jwt() -> str:
    return _jwt("https://api.sandbox.tastyworks.com")


def _row(**over) -> ResolvedEntry:
    base = dict(time=time(9, 30), contracts=1, target_premium=D("5.20"), wing_width=D("50"),
                stop_loss_pct=95, stop_basis="total_credit", stop_rebate_markup=D("0.00"),
                min_short_premium=D("1.00"), min_total_credit=D("2.00"), probe_down_max=25,
                strike_method="premium", short_delta_target=D("0.10"))
    return ResolvedEntry(**{**base, **over})


def _condor(n: int, contracts: int, credit: D) -> Condor:
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5940"), call_long=D("6110"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=credit, min_total_credit=D("2.00"),
                  expiration=WHEN.date(), contracts=contracts)


async def _gates() -> GateSnapshot:
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


def _live_comp():
    """A REAL `LiveComposition` (composition/live.py, unmodified) with only its
    network-facing broker swapped for the offline `FakeBroker`. `_on_filled`,
    `_shorts`, `.execute`, `.protect` are all the actual production objects and
    methods — the fail-first sabotage step targets `_on_filled` in that same
    file, so the test must be exercising IT, not a hand-rolled stand-in."""
    comp = LiveComposition(clock=FastClock(WHEN), ticks=SPX,
                           provider_secret="s", refresh_token=_cert_jwt())
    fake = FakeBroker()
    fake.autofill(IS_CONDOR)
    comp.broker = fake
    comp.execute._broker = fake
    comp.protect._broker = fake
    comp.state.armed = True
    comp.state.confirm_live = True
    return comp, fake


def _fire_scheduled(markup: D, *, credit: D = CREDIT, day: str = "2026-07-13"):
    """Drives the REAL scheduled path: a schedule row (with its own
    `stop_rebate_markup`) -> `LiveRuntime.run_day` -> `ExecuteEntryAttempt.attempt`
    (real ladder, real fill) -> `LiveComposition._on_filled` -> `ProtectPosition.
    protect` -> the broker-resting stop + the journaled `StopPlaced`."""
    comp, fake = _live_comp()
    row = ScheduledRow(WHEN, _row(stop_rebate_markup=markup))

    async def selector(when, n, config=None):
        return _condor(n, config.contracts if config else 1, credit), None

    rt = LiveRuntime(comp=comp, selector=selector, market_gates=_gates)
    filled = asyncio.run(rt.run_day(day, [row]))
    assert filled == 1, "setup precondition failed: the entry must fill for this test to mean anything"
    return comp, fake, f"{day}#1"


# --- 1. the operator's exact live vector: markup 0.30 reaches the broker -------

def test_the_operators_exact_live_vector_lands_at_the_broker():
    """credit 5.20, stop_loss_pct 95, stop_rebate_markup 0.30, basis total_credit
    (the operator's real 2026-07-13 live row). The placed stop's trigger and the
    journaled StopPlaced.markup must reflect the ROW's 0.30 buffer -- computed
    via the real stop_policy/tick table, never a hardcoded literal."""
    comp, fake, entry_id = _fire_scheduled(D("0.30"))
    expected = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                           markup=D("0.30"), total_net_credit=CREDIT)

    placed = [e for e in comp.events if isinstance(e, StopPlaced) and e.entry_id == entry_id]
    assert {e.side for e in placed} == {"PUT", "CALL"}                 # 4. both shorts
    assert all(e.trigger == expected for e in placed)
    assert all(e.markup == D("0.30") for e in placed)

    # Not merely journaled -- the broker itself is resting stops at this trigger.
    working = asyncio.run(fake.working_orders())
    stop_orders = [o for o in working if o.intent.kind == "stop"]
    assert len(stop_orders) == 2
    assert all(o.intent.stop_trigger == expected for o in stop_orders)


# --- 2. the zero-buffer control: same row otherwise --------------------------

def test_zero_buffer_control_same_row_otherwise():
    """The same row, `stop_rebate_markup = 0.00` -- the control that proves the
    0.30 result above is the MARKUP's effect, not some other coincidence of the
    fixture (e.g. the pct or the tick table alone producing 5.20)."""
    comp, fake, entry_id = _fire_scheduled(D("0.00"))
    expected = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                           markup=D("0.00"), total_net_credit=CREDIT)

    placed = [e for e in comp.events if isinstance(e, StopPlaced) and e.entry_id == entry_id]
    assert len(placed) == 2
    assert all(e.trigger == expected for e in placed)
    assert all(e.markup == D("0.00") for e in placed)


# --- 3. the delta is exactly the buffer, for a credit with no flooring artefact -

def test_the_delta_is_exactly_the_buffer_at_this_credit():
    """5.20 @ 95% -> 4.90 with no markup, 5.20 with a 0.30 markup: a clean 0.30
    delta because nothing gets eaten by tick-flooring at this particular credit.

    NOTE: this exact equality is a property of credit=5.20, not a general law --
    STP-02b adds the markup BEFORE flooring, so flooring can absorb part of a
    buffer at other credits (see TC-STP-14 scenario 1: raw 2.99 floors to 2.95,
    "eating" 0.04 of a 0.50 markup). The invariant that must ALWAYS hold is
    "the markup is applied before flooring" -- never "the delta always equals
    the markup"."""
    with_buffer = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                              markup=D("0.30"), total_net_credit=CREDIT)
    without = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                           markup=D("0.00"), total_net_credit=CREDIT)
    assert with_buffer == D("5.20") and without == D("4.90")
    assert with_buffer - without == D("0.30")


# --- 5. the manual/ad-hoc fire path gets the same buffer ----------------------

def test_manual_fire_also_lands_the_raised_trigger():
    """The manual trade card's ▶ press goes through `manual_entry._stop(row)`
    (StopParams(markup=row.stop_rebate_markup)), a SEPARATE call site from
    `ScheduledRow.stop` -- it must be pinned independently, or a regression
    confined to the manual path would slip past every scheduled-path test above."""
    comp, fake = _live_comp()
    row = _row(stop_rebate_markup=D("0.30"))

    async def selector(when, n, config=None, put_floor=None, call_floor=None):
        return _condor(n, config.contracts if config else 1, CREDIT), None

    manual = ManualEntry(comp, selector, _gates, day=lambda: "2026-07-13")
    out = asyncio.run(manual.fire(press_id="p1", entry_number=1, row=row, confirmed=True))

    assert out["result"] == "filled"
    expected = stop_trigger(StopBasis.TOTAL_CREDIT, ticks=SPX, pct=D("95"),
                           markup=D("0.30"), total_net_credit=CREDIT)
    placed = [e for e in comp.events if isinstance(e, StopPlaced)]
    assert {e.side for e in placed} == {"PUT", "CALL"}
    assert all(e.trigger == expected and e.markup == D("0.30") for e in placed)
