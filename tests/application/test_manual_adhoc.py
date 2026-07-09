"""ENT-11/UI-25: ad-hoc manual entry — Simulate (read-only) and the 101+ ad-hoc
numbering lane. Fire itself is the IDENTICAL ENT-09 pipeline (test_manual_fire_
shield.py and test_api_schedule_and_fire.py already cover attempt/hand-off); this
file covers what is new: `ManualEntry.simulate`, `_adhoc_row`, `_next_adhoc_number`.
"""
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest
from fastapi import HTTPException

from meic.adapters.api.app import _adhoc_row, _next_adhoc_number
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.manual_entry import ManualEntry
from meic.application.persistent_state import PersistentState
from meic.domain.events import CondorFilled, EntrySkipped
from meic.domain.ticks import TickRung, TickTable

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
NOW = datetime(2026, 7, 10, 10, 7, tzinfo=timezone.utc)


class _Clock:
    def now(self):
        return NOW


class _Comp:
    """Only what `simulate` touches — no broker, no `execute`, because a
    simulation must never reach either (ENT-11/UI-25: read-only)."""

    def __init__(self):
        self.events: list = []
        self.clock = _Clock()


def _condor(n=0, contracts=1):
    return Condor(entry_number=n, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5940"), call_long=D("6110"),
                  put_short_mid=D("3.10"), call_short_mid=D("2.90"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=date(2026, 7, 10), contracts=contracts)


# --- ManualEntry.simulate (ENT-11/UI-25) -------------------------------------

def test_simulate_returns_strikes_credit_and_worst_case_read_only():
    async def scenario():
        comp = _Comp()

        async def selector(when, n, config=None):
            assert n == 0, "ENT-11/UI-25: simulate must probe with entry_number 0"
            return _condor(n, contracts=config.contracts if config else 1), None

        manual = ManualEntry(comp, selector, market_gates=None)
        row = _adhoc_row({"contracts": 3, "target_premium": "3.00", "wing_width": "50"})
        out = await manual.simulate(row)

        assert out["result"] == "ok"
        assert out["put_short"] == "5990" and out["put_long"] == "5940"
        assert out["call_short"] == "6060" and out["call_long"] == "6110"
        assert out["put_mid"] == "3.10" and out["call_mid"] == "2.90"
        assert out["net_credit"] == "4.00"
        assert out["contracts"] == 3
        assert out["worst_case"] == str(ExecuteEntryAttempt.worst_case(_condor(0, 3)))
        assert "simulation" in out["estimate_note"]

        # ENT-11/UI-25: a simulation appends NO event and places no order.
        assert comp.events == []

    import asyncio
    asyncio.run(scenario())


def test_simulate_surfaces_the_selector_skip_reason_verbatim():
    async def scenario():
        comp = _Comp()

        async def selector(when, n, config=None):
            return None, "incomplete_chain"

        manual = ManualEntry(comp, selector, market_gates=None)
        row = _adhoc_row({})
        out = await manual.simulate(row)

        assert out == {"result": "skipped", "reason": "incomplete_chain"}
        assert comp.events == []  # still nothing recorded on a skip

    import asyncio
    asyncio.run(scenario())


# --- _next_adhoc_number (ENT-11(3)) -------------------------------------------

def test_next_adhoc_number_starts_at_101_when_none_exist():
    assert _next_adhoc_number([], "2026-07-10") == 101


def test_next_adhoc_number_increments_past_an_existing_fill():
    events = [CondorFilled(entry_id="2026-07-10#101", net_credit=D("4.00"))]
    assert _next_adhoc_number(events, "2026-07-10") == 102


def test_next_adhoc_number_increments_past_an_existing_skip():
    events = [EntrySkipped(date="2026-07-10", entry_number=101, reason="blocked")]
    assert _next_adhoc_number(events, "2026-07-10") == 102


def test_next_adhoc_number_ignores_schedule_lane_numbers_below_101():
    events = [
        CondorFilled(entry_id="2026-07-10#1", net_credit=D("4.00")),
        CondorFilled(entry_id="2026-07-10#6", net_credit=D("4.00")),
        EntrySkipped(date="2026-07-10", entry_number=3, reason="max_entries"),
    ]
    assert _next_adhoc_number(events, "2026-07-10") == 101


def test_next_adhoc_number_ignores_other_days():
    events = [CondorFilled(entry_id="2026-07-09#105", net_credit=D("4.00"))]
    assert _next_adhoc_number(events, "2026-07-10") == 101


# --- _adhoc_row (ENT-11(4)/UI-03) --------------------------------------------

def test_adhoc_row_rejects_out_of_range_contracts_with_422_shaped_errors():
    with pytest.raises(HTTPException) as exc:
        _adhoc_row({"contracts": 11})
    assert exc.value.status_code == 422
    errors = exc.value.detail["errors"]
    assert any(e["field"] == "contracts" and e["reason"] == "out_of_range" for e in errors)


def test_adhoc_row_resolves_defaults_for_absent_fields():
    row = _adhoc_row({})
    assert row.contracts == 1
    assert row.target_premium == D("3.00")
    assert row.wing_width == D("50")
    assert row.stop_loss_pct == 95
