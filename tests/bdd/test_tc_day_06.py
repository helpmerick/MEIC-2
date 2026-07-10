"""Hand-written step definitions for TC-DAY-06 — backend-authoritative entry
time validation: the 24-hour military format gate, dot-canonicalisation, and
the RTH (market hours) window. Drives the real ScheduleService
(backend/src/meic/application/schedule_service.py) exactly like
tests/application/test_schedule_service.py does — these steps re-express
already unit-tested behaviours through the Gherkin.
"""
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.application.schedule_service import ScheduleService

scenarios("../features/TC-DAY-06.feature")


def _svc() -> ScheduleService:
    return ScheduleService(PersistentState(InMemoryStateStore()))


def _row(t):
    return {"time": t}


@pytest.fixture
def world():
    return {}


# --- Scenario Outline: non-military formats are rejected per row --------------

@when(parsers.parse('a schedule row\'s time is "{bad}"'))
def _(world, bad):
    world["out"] = _svc().save([_row(bad)])


@then('validation rejects it with reason "not_24h_military"')
def _(world):
    out = world["out"]
    assert out["result"] == "invalid"
    assert any(e["reason"] == "not_24h_military" for e in out["errors"])


# --- Scenario: valid formats pass and dots canonicalise ------------------------

@then("09:32, 9:32, 15:30 and 23:59 pass the format gate")
def _(world):
    # 23:59 passes the FORMAT gate but fails the RTH window (checked below in
    # the separate scenario) -- so assert absence of not_24h_military here,
    # never full save success.
    svc = _svc()
    for t in ("09:32", "9:32", "15:30", "23:59"):
        errs = svc.validate([_row(t)])
        assert not any(e.reason == "not_24h_military" for e in errs)


@then("11.53 persists as 11:53 and 9.32 persists as 09:32")
def _(world):
    out1 = _svc().save([_row("11.53")])
    assert out1["result"] == "saved" and out1["rows"][0]["time"] == "11:53"

    out2 = _svc().save([_row("9.32")])
    assert out2["result"] == "saved" and out2["rows"][0]["time"] == "09:32"


# --- Scenario: the RTH window is enforced on the value -------------------------

@then('08:00 and 16:30 are rejected with reason "outside_market_hours"')
def _(world):
    for t in ("08:00", "16:30"):
        out = _svc().save([_row(t)])
        assert out["result"] == "invalid"
        assert any(e["reason"] == "outside_market_hours" for e in out["errors"])


@then("09:30 (the open edge) saves")
def _(world):
    out = _svc().save([_row("09:30")])
    assert out["result"] == "saved"


@then("the format and window checks are backend-authoritative")
def _(world):
    # Both gates run entirely inside ScheduleService with no UI in the loop at
    # all -- proof they are backend-authoritative, not merely an inline hint
    # (frontend/src/time.ts's isMilitaryTime/withinMarketHours are display-only).
    svc = _svc()
    assert svc.save([_row("1:53pm")])["result"] == "invalid"   # format gate
    assert svc.save([_row("08:00")])["result"] == "invalid"    # window gate
    assert svc.save([_row("09:30")])["result"] == "saved"
