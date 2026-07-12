"""ProtectPosition — STP-01/02/04/06 placement, verification, escalation."""
import asyncio
from decimal import Decimal as D

import pytest

from meic.application.close_entry import CloseEntry
from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.composition.close_assembly import DEFAULT_CLOSE_PRICE, assemble_close_inputs
from meic.domain.events import (
    CondorFilled,
    EntryClosed,
    EntryClosedInfeasible,
    FilledLeg,
    SideUnprotected,
    StopConfirmed,
    StopPlaced,
)
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker, Scripted
from tests.harness.fake_clock import ET, FakeClock

from datetime import datetime

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


class RecordingAlerts:
    def __init__(self):
        self.calls = []

    def alert(self, level, message, **ctx):
        self.calls.append((level, message, ctx))


def _protect(broker, events, alerts, **kw):
    clock = FakeClock(datetime(2026, 7, 6, 10, 0, tzinfo=ET))
    return ProtectPosition(broker, clock, alerts, events, SPX, **kw)


def test_total_credit_places_two_stops_on_shorts_only():
    """STP-01/06: two buy-to-close stop-market (TIF Day) on the shorts, none on longs."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    p = _protect(broker, events, alerts)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000"), ShortLeg("CALL", D("2.00"), D("0.50"), symbol="SPXW  260707C06060000")]
    result = asyncio.run(p.protect(entry_id="e1", basis=StopBasis.TOTAL_CREDIT,
                                   shorts=shorts, pct=D("95"), total_net_credit=D("4.00")))
    assert result.outcome == "PROTECTED"
    assert result.triggers == {"PUT": D("3.80"), "CALL": D("3.80")}  # shared level
    placed = asyncio.run(broker.working_orders())
    assert len(placed) == 2
    assert all(o.intent.legs[0].action == "buy_to_close" and o.intent.order_type == "stop_market"
               and o.intent.tif == "Day" for o in placed)
    assert all(o.stop_leg_key.startswith("short_") for o in placed)  # STP-06
    assert sum(isinstance(e, StopConfirmed) for e in events) == 2


def test_post_fill_infeasible_closes_instead_of_placing_suicidal_stop():
    """STP-02c checkpoint 2: trigger below a short's fill -> close via CLS
    (initiator infeasible_stop), never place a stop that fires at birth."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    closed = []

    async def close_cb(entry_id, initiator):
        closed.append((entry_id, initiator))

    p = _protect(broker, events, alerts, close_entry=close_cb)
    # net credit 2.00 @ 95 -> trigger 1.90, below the 3.00 short
    shorts = [ShortLeg("PUT", D("3.00"), D("1.50"), symbol="SPXW  260707P05990000"), ShortLeg("CALL", D("2.00"), D("1.50"), symbol="SPXW  260707C06060000")]
    result = asyncio.run(p.protect(entry_id="e2", basis=StopBasis.TOTAL_CREDIT,
                                   shorts=shorts, pct=D("95"), total_net_credit=D("2.00")))
    assert result.outcome == "INFEASIBLE_CLOSED"
    assert closed == [("e2", "infeasible_stop")]
    assert any(isinstance(e, EntryClosedInfeasible) for e in events)
    assert asyncio.run(broker.working_orders()) == []  # no stop was placed


def test_unprotected_escalation_after_retries_exhausted():
    """STP-04: broker rejects every placement -> UNPROTECTED, flatten + alert."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    broker.script_submit(*[Scripted("reject", payload={"reason": "x"}) for _ in range(6)])
    flattened = []

    async def close_cb(entry_id, initiator):
        flattened.append((entry_id, initiator))

    p = _protect(broker, events, alerts, stop_retry_attempts=3, close_entry=close_cb)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000")]
    result = asyncio.run(p.protect(entry_id="e3", basis=StopBasis.TOTAL_CREDIT,
                                   shorts=shorts, pct=D("95"), total_net_credit=D("4.00")))
    assert result.outcome == "UNPROTECTED_FLATTENED"
    assert any(isinstance(e, SideUnprotected) for e in events)
    assert any(level == "critical" for level, _, _ in alerts.calls)
    assert flattened == [("e3", "unprotected")]


def test_stop_placed_journals_the_markup_in_force():
    """RPT-07 long recovery (2026-07-11, operator ruling): StopPlaced carries
    the STP-02b markup used to compute THIS stop's trigger, so a later
    realized long-sale recovery can be compared against it (NLE-06)."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    p = _protect(broker, events, alerts)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000")]
    asyncio.run(p.protect(entry_id="e9", basis=StopBasis.TOTAL_CREDIT, shorts=shorts,
                          pct=D("95"), markup=D("0.10"), total_net_credit=D("4.00")))

    placed = [e for e in events if isinstance(e, StopPlaced)]
    assert len(placed) == 1
    assert placed[0].markup == D("0.10")


def test_stop_placed_markup_defaults_to_zero_not_none_when_unspecified():
    """`protect()`'s own `markup` parameter already defaults to Decimal("0")
    (unchanged) -- StopPlaced.markup carries that same zero, not None. None
    is reserved for events recorded before this field existed at all (event-
    store replay), never for "caller didn't pass one"."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    p = _protect(broker, events, alerts)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000")]
    asyncio.run(p.protect(entry_id="e10", basis=StopBasis.TOTAL_CREDIT, shorts=shorts,
                          pct=D("95"), total_net_credit=D("4.00")))

    placed = [e for e in events if isinstance(e, StopPlaced)]
    assert placed[0].markup == D("0")


def test_short_premium_basis_per_side_triggers():
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    p = _protect(broker, events, alerts)
    # short_premium: put 3.00 * 1.95 = 5.85 floor -> 5.80 (0.10 tick)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000")]
    result = asyncio.run(p.protect(entry_id="e4", basis=StopBasis.SHORT_PREMIUM,
                                   shorts=shorts, pct=D("95")))
    assert result.triggers["PUT"] == D("5.80")  # floor(5.85) in 0.10 regime


def test_confirmed_qty_matches_both_fake_order_id_and_live_id_shapes():
    """Regression (2026-07-09): live working orders are SDK PlacedOrder objects
    keyed by `.id`; our SimOrder/FakeOrder use `.order_id`. Matching only one left
    a live stop unconfirmed -> a PROTECTED condor sent down the UNPROTECTED path."""
    from types import SimpleNamespace
    live_o = SimpleNamespace(id="7001", legs=[SimpleNamespace(quantity=2), SimpleNamespace(quantity=2)])
    fake_o = SimpleNamespace(order_id="7002", intent=SimpleNamespace(contracts=1))

    class _B:
        async def working_orders(self):
            return [live_o, fake_o]

    p = _protect(_B(), [], RecordingAlerts())
    assert asyncio.run(p._confirmed_qty("7001")) == 2    # live matched by .id
    assert asyncio.run(p._confirmed_qty("7002")) == 1    # fake matched by .order_id
    assert asyncio.run(p._confirmed_qty("9999")) is None


# --- STP-04 AUTO-FLATTEN wiring (the hook existed unwired for weeks) -----------

def _condor_filled_legs():
    """The whole entry's broker-reported legs (ORD-09) — both sides, so a
    whole-entry auto-flatten close has something real to close on each."""
    return (
        FilledLeg(symbol="SPXW  260707P05940000", right="P", role="long", qty=1, price=D("0.50")),
        FilledLeg(symbol="SPXW  260707P05990000", right="P", role="short", qty=1, price=D("3.00")),
        FilledLeg(symbol="SPXW  260707C06060000", right="C", role="short", qty=1, price=D("2.00")),
        FilledLeg(symbol="SPXW  260707C06110000", right="C", role="long", qty=1, price=D("0.40")),
    )


def test_unprotected_flatten_wires_the_real_close_entry_with_real_legs_and_stop_ids():
    """STP-04 AUTO-FLATTEN regression: ProtectPosition accepted a close_entry
    callback for weeks with nothing wiring it in live/paper composition — the
    critical alert fired and nothing further happened. Here the callback
    mirrors the composition's real wiring (assemble_close_inputs -> the REAL
    CloseEntry, exactly like live.py/paper.py `_auto_flatten_entry`) and must
    actually close the entry's broker-reported legs, not a stand-in."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    events.append(CondorFilled(entry_id="e6", net_credit=D("4.00"), legs=_condor_filled_legs()))
    broker.script_submit(*[Scripted("reject", payload={"reason": "x"}) for _ in range(3)])  # exhaust retries
    close_entry = CloseEntry(broker, events)

    async def close_cb(entry_id, initiator):
        inputs = await assemble_close_inputs(events, broker, entry_id)
        assert inputs is not None
        legs, stop_ids = inputs
        await close_entry.close(entry_id, initiator, resting_stop_ids=stop_ids,
                                live_legs=legs, close_price=DEFAULT_CLOSE_PRICE)

    p = _protect(broker, events, alerts, stop_retry_attempts=3, close_entry=close_cb)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000")]
    result = asyncio.run(p.protect(entry_id="e6", basis=StopBasis.TOTAL_CREDIT,
                                   shorts=shorts, pct=D("95"), total_net_credit=D("4.00")))

    assert result.outcome == "UNPROTECTED_FLATTENED"
    assert [e for e in events if isinstance(e, EntryClosed)] == \
        [EntryClosed(entry_id="e6", initiator="unprotected")]
    # No stop ever confirmed (every placement was rejected) -> CLS-01(3): a
    # direct marketable close on every recorded leg, not a replace race. All
    # four of the entry's real legs (both sides) got a close order — the
    # OPEN ITEM in protect_position.py: unprotected always flattens the WHOLE
    # entry today, not just the side that went unprotected.
    working = asyncio.run(broker.working_orders())
    closed_symbols = {o.intent.legs[0].symbol for o in working if o.intent.kind == "close"}
    assert closed_symbols == {
        "SPXW  260707P05940000", "SPXW  260707P05990000",
        "SPXW  260707C06060000", "SPXW  260707C06110000",
    }


def test_stop_quantity_mismatch_flattens_the_entry():
    """STP-01 (v1.45): a stop that confirms WORKING but at the wrong size is
    naked, not retried, not resized — UNPROTECTED_FLATTENED immediately, same
    escalation path as a stop that never confirmed at all."""
    from types import SimpleNamespace

    class _WrongSizeBroker:
        """submit() confirms, but the WORKING order reported back is
        undersized: a 2-contract stop rests at 1 -> 1 contract naked."""

        async def submit(self, order):
            return "O-1"

        async def working_orders(self):
            return [SimpleNamespace(order_id="O-1", intent=SimpleNamespace(contracts=1))]

    events, alerts = [], RecordingAlerts()
    flattened = []

    async def close_cb(entry_id, initiator):
        flattened.append((entry_id, initiator))

    p = _protect(_WrongSizeBroker(), events, alerts, close_entry=close_cb)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000")]
    result = asyncio.run(p.protect(entry_id="e7", basis=StopBasis.TOTAL_CREDIT,
                                   shorts=shorts, pct=D("95"), total_net_credit=D("4.00"),
                                   contracts=2))

    assert result.outcome == "UNPROTECTED_FLATTENED"
    assert any(isinstance(e, SideUnprotected) for e in events)
    assert any("naked" in msg for _, msg, _ in alerts.calls)
    assert flattened == [("e7", "unprotected")]


def test_auto_flatten_callback_survives_an_empty_leg_book_without_crashing():
    """If the broker never reported any legs for this entry (no CondorFilled
    landed yet, or a stale entry_id), assemble_close_inputs returns None — the
    callback must alert and return, never raise, never fabricate legs."""
    broker, events, alerts = FakeBroker(), [], RecordingAlerts()
    broker.script_submit(*[Scripted("reject", payload={"reason": "x"}) for _ in range(3)])

    async def close_cb(entry_id, initiator):
        inputs = await assemble_close_inputs(events, broker, entry_id)
        if inputs is None or not inputs[0]:
            alerts.alert("critical", "STP-04 auto-flatten: no legs recorded", entry_id=entry_id)
            return
        raise AssertionError("should never reach here: no legs were recorded")

    p = _protect(broker, events, alerts, stop_retry_attempts=3, close_entry=close_cb)
    shorts = [ShortLeg("PUT", D("3.00"), D("0.50"), symbol="SPXW  260707P05990000")]
    result = asyncio.run(p.protect(entry_id="e8", basis=StopBasis.TOTAL_CREDIT,
                                   shorts=shorts, pct=D("95"), total_net_credit=D("4.00")))

    assert result.outcome == "UNPROTECTED_FLATTENED"     # did not crash
    assert any("no legs recorded" in msg for _, msg, _ in alerts.calls)
    assert not [e for e in events if isinstance(e, EntryClosed)]  # nothing closed
