"""application.settlement_capture -- EOD-01 v1.59 LIVE settlement capture
(operator-ratified, 2026-07-09 escalation): "Settlement cash is
BROKER-JOURNALED, never merely computed." Distinct from
tests/application/test_backfill.py (RPT-16's ONE-TIME import of PRE-journal
broker history) -- this is the ongoing live-path capture, attributed by
symbol against the day's OWN CondorFilled leg book rather than an
operator-supplied order-id set.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal as D

from meic.application.settlement_capture import capture_settlements
from meic.domain.events import CondorFilled, FilledLeg, ForeignDetected, SettlementRecorded

DAY = "2026-07-09"

P7535 = "SPXW  260709P07535000"
P7510 = "SPXW  260709P07510000"
C7540 = "SPXW  260709C07540000"
C7565 = "SPXW  260709C07565000"


@dataclass
class FakeSettlement:
    """Mirrors the tastytrade SDK's Receive-Deliver Transaction shape closely
    enough for capture_settlements's field reads (see application/backfill.py's
    docstring for the exact SDK field mapping this mirrors)."""
    symbol: str | None
    transaction_sub_type: str | None
    value: D | None
    net_value: D | None
    price: D | None = None
    quantity: D | None = D("1")
    executed_at: datetime = datetime(2026, 7, 10, 2, 0, tzinfo=timezone.utc)


class FakeBrokerReads:
    def __init__(self, rows: list[FakeSettlement]) -> None:
        self._rows = rows
        self.calls: list[str] = []

    async def day_settlements(self, day: str):
        self.calls.append(day)
        return self._rows


def _condor_legs() -> tuple[FilledLeg, ...]:
    return (
        FilledLeg(symbol=P7535, right="P", role="short", qty=1, price=D("2.20")),
        FilledLeg(symbol=P7510, right="P", role="long", qty=1, price=D("0.40")),
        FilledLeg(symbol=C7540, right="C", role="short", qty=1, price=D("2.15")),
        FilledLeg(symbol=C7565, right="C", role="long", qty=1, price=D("0.35")),
    )


def _real_settlement_rows() -> list[FakeSettlement]:
    return [
        FakeSettlement(C7540, "Cash Settled Assignment", D("-364.0"), D("-369.0"), price=D("7540.0")),
        FakeSettlement(P7535, "Expiration", D("0"), D("0")),
        FakeSettlement(P7510, "Expiration", D("0"), D("0")),
        FakeSettlement(C7565, "Expiration", D("0"), D("0")),
    ]


def _now_iso() -> str:
    return "2026-07-10T09:00:00-04:00"


def _run(events, broker, day=DAY, **kw):
    return asyncio.run(capture_settlements(events, broker, day, now_iso=_now_iso, **kw))


def _filled(entry_id=f"{DAY}#1", legs=None, net_credit=D("3.60"), fee=D("0.0488")):
    return CondorFilled(entry_id=entry_id, net_credit=net_credit, fee=fee,
                        legs=legs if legs is not None else _condor_legs())


# --- attribution by symbol against the day's own leg book --------------------

def test_captures_and_attributes_the_exact_real_2026_07_09_shapes():
    events = [_filled()]
    result = _run(events, FakeBrokerReads(_real_settlement_rows()))

    assert result == {"result": "captured", "captured": 4, "ambiguous_settlements": 0}
    recorded = [e for e in events if isinstance(e, SettlementRecorded)]
    assert len(recorded) == 4
    assert all(e.entry_id == f"{DAY}#1" for e in recorded)
    cash = next(e for e in recorded if e.symbol == C7540)
    assert cash.value == D("-369.0")
    assert cash.fee == D("5.0")
    assert cash.sub_type == "Cash Settled Assignment"
    assert cash.price == D("7540.0")
    assert cash.quantity == 1
    assert cash.source == "tastytrade_receive_deliver"
    zeros = [e for e in recorded if e.sub_type == "Expiration"]
    assert {e.symbol for e in zeros} == {P7535, P7510, C7565}
    assert all(e.value == D("0") and e.fee == D("0") for e in zeros)


def test_a_settlement_for_a_symbol_we_never_traded_is_ignored():
    """Not one of today's own entries -- not ours to capture, and never
    counted ambiguous (it simply isn't ours)."""
    events = [_filled()]
    foreign_row = FakeSettlement("SPXW  260709P05500000", "Expiration", D("0"), D("0"))
    result = _run(events, FakeBrokerReads([foreign_row]))

    assert result == {"result": "captured", "captured": 0, "ambiguous_settlements": 0}
    assert not any(isinstance(e, SettlementRecorded) for e in events)


def test_settlement_scoped_to_a_different_day_is_ignored():
    """The leg book is built from THIS day's own entries only -- an entry
    filed under a different day never attributes a symbol here."""
    events = [_filled(entry_id="2026-07-08#1")]
    result = _run(events, FakeBrokerReads(_real_settlement_rows()), day=DAY)
    assert result == {"result": "captured", "captured": 0, "ambiguous_settlements": 0}


# --- OWN-03 shared-symbol guard ------------------------------------------------

def test_own_03_foreign_quarantined_symbol_is_withheld_and_counted_ambiguous():
    events = [_filled(), ForeignDetected(symbol=C7540)]
    result = _run(events, FakeBrokerReads(_real_settlement_rows()))

    assert result == {"result": "captured", "captured": 3, "ambiguous_settlements": 1}
    assert not any(isinstance(e, SettlementRecorded) and e.symbol == C7540 for e in events)
    symbols = {e.symbol for e in events if isinstance(e, SettlementRecorded)}
    assert symbols == {P7535, P7510, C7565}


def test_a_symbol_claimed_by_two_of_todays_own_entries_is_ambiguous():
    """Never happens in the ordinary one-strike-per-entry case, but never
    guessed at either: a symbol two of today's own entries both claim cannot
    be attributed to either one."""
    shared_leg = FilledLeg(symbol=C7540, right="C", role="short", qty=1, price=D("2.15"))
    events = [
        _filled(entry_id=f"{DAY}#1", legs=(shared_leg,)),
        _filled(entry_id=f"{DAY}#2", legs=(shared_leg,)),
    ]
    row = FakeSettlement(C7540, "Cash Settled Assignment", D("-364.0"), D("-369.0"))
    result = _run(events, FakeBrokerReads([row]))

    assert result == {"result": "captured", "captured": 0, "ambiguous_settlements": 1}
    assert not any(isinstance(e, SettlementRecorded) for e in events)


# --- transaction-level idempotency --------------------------------------------

def test_rerunning_after_full_capture_is_a_true_no_op():
    events = [_filled()]
    broker = FakeBrokerReads(_real_settlement_rows())
    r1 = _run(events, broker)
    assert r1["captured"] == 4
    before = list(events)

    r2 = _run(events, broker)
    assert r2 == {"result": "captured", "captured": 0, "ambiguous_settlements": 0}
    assert events == before


def test_idempotency_is_scoped_per_day():
    other_day_settlement = SettlementRecorded(
        entry_id="2026-07-08#1", day="2026-07-08", at=_real_settlement_rows()[0].executed_at.isoformat(),
        symbol=C7540, sub_type="Cash Settled Assignment", quantity=1, price=D("7540.0"),
        value=D("-369.0"), fee=D("5.0"))
    events = [other_day_settlement, _filled()]
    result = _run(events, FakeBrokerReads(_real_settlement_rows()), day=DAY)
    assert result["captured"] == 4  # the other day's record never blocks today's


# --- honest absence, never fabricated -----------------------------------------

def test_a_settlement_row_with_no_net_value_is_skipped_not_fabricated_zero():
    events = [_filled()]
    row = FakeSettlement(C7540, "Cash Settled Assignment", None, None)
    result = _run(events, FakeBrokerReads([row]))
    assert result == {"result": "captured", "captured": 0, "ambiguous_settlements": 0}
    assert not any(isinstance(e, SettlementRecorded) for e in events)


def test_fee_is_none_when_the_broker_reported_no_raw_value():
    events = [_filled()]
    row = FakeSettlement(C7540, "Cash Settled Assignment", None, D("-369.0"))
    _run(events, FakeBrokerReads([row]))
    rec = next(e for e in events if isinstance(e, SettlementRecorded))
    assert rec.value == D("-369.0")
    assert rec.fee is None


# --- the computed-settle cross-check is a deliberate no-op unless supplied ----

def test_computed_settle_cross_check_alerts_on_disagreement():
    class _FakeAlerts:
        def __init__(self):
            self.alerts = []

        def alert(self, level, message, **context):
            self.alerts.append({"level": level, "message": message, **context})

    events = [_filled()]
    alerts = _FakeAlerts()
    _run(events, FakeBrokerReads(_real_settlement_rows()),
        computed_settle=lambda symbol, day: D("-400.00") if symbol == C7540 else None,
        alerts=alerts)
    critical = [a for a in alerts.alerts if a["level"] == "critical"]
    assert len(critical) == 1
    assert C7540 in critical[0]["message"] and DAY in critical[0]["message"]
    assert critical[0]["computed"] == "-400.00" and critical[0]["broker"] == "-369.0"
    # The row is still captured -- the cross-check only alerts, never blocks.
    assert any(isinstance(e, SettlementRecorded) and e.symbol == C7540 for e in events)


def test_computed_settle_cross_check_is_silent_with_no_argument():
    """No computed-settle engine exists in this codebase today -- omitting
    the argument (every current caller) must never alert or raise."""
    events = [_filled()]
    result = _run(events, FakeBrokerReads(_real_settlement_rows()))
    assert result["captured"] == 4
