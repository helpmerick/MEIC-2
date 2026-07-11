"""Hand-written step definitions for TC-RPT-10 — RPT-16 historical backfill,
end to end through the real `/reports/*` API. RPT-16 itself (application/
backfill.py) is ALREADY BUILT and unit-tested directly
(tests/application/test_backfill.py); this file is BINDING ONLY -- it drives
the same real fixtures through `POST /reports/backfill/{day}` and asserts the
`/reports/summary` and `/reports/day/{iso_date}` payloads a panel would
actually render, proving the wiring rather than re-testing backfill_day's
internals a second time.

Reuses tests/application/test_backfill.py's exact real 2026-07-09 shapes
(`FakeBrokerReads`, `FakeTransaction`, `_real_trade_legs`, `_real_settlements`)
-- the SAME broker Transaction fixtures the unit suite already pins, never a
parallel re-implementation.
"""
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, scenarios, then

from meic.adapters.api.app import create_app
from meic.adapters.api.reports import ReportingConfig
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.domain.events import (
    CondorFilled,
    ExternalFillImported,
    FilledLeg,
    SideExpired,
)
from tests.application.test_backfill import (
    FakeBrokerReads,
    FakeTransaction,
    _real_settlements,
    _real_trade_legs,
)

scenarios("../features/TC-RPT-10.feature")

DAY = "2026-07-09"
ORDER_ID = "482390058"


def _rpt10_client(events: list, broker_reads, *, capital_base: D | None = D("50000")):
    state = PersistentState(InMemoryStateStore())
    app = create_app(
        state, events, panel_origin="http://127.0.0.1",
        reporting_config=ReportingConfig(capital_base=capital_base),
        backfill_broker_reads=broker_reads,
    )
    return TestClient(app)


def _real_entry_on_another_day() -> list:
    """A completely ordinary bot day (2026-07-08) held to expiry both
    sides -- FULL_EXPIRY taxonomy, a real Sharpe/expectancy/streak sample --
    so the imported day's exclusion from those figures is provable by
    comparison, not just a vacuous "nothing there at all" count."""
    legs = (
        FilledLeg(symbol="SPXW  260708P07505000", right="P", role="long", qty=1, price=D("0.10")),
        FilledLeg(symbol="SPXW  260708P07525000", right="P", role="short", qty=1, price=D("1.60")),
        FilledLeg(symbol="SPXW  260708C07550000", right="C", role="short", qty=1, price=D("1.60")),
        FilledLeg(symbol="SPXW  260708C07570000", right="C", role="long", qty=1, price=D("0.10")),
    )
    return [
        CondorFilled(entry_id="2026-07-08#1", net_credit=D("3.00"), legs=legs),
        SideExpired(entry_id="2026-07-08#1", side="PUT"),
        SideExpired(entry_id="2026-07-08#1", side="CALL"),
    ]


@pytest.fixture
def world():
    return {}


# --- Scenario: Imported days render cash and are excluded from quality metrics -----

@given("a backfilled day with fills netting +355.12 and an imported settlement of -369.00")
def _(world):
    events = _real_entry_on_another_day()
    broker = FakeBrokerReads(_real_trade_legs(), _real_settlements())
    client = _rpt10_client(events, broker)

    r = client.post(f"/reports/backfill/{DAY}", json={"order_ids": [ORDER_ID]})
    assert r.status_code == 200
    world.update(events=events, client=client, backfill_result=r.json())


@then("the day renders net -13.88 with the broker-imported badge")
def _(world):
    day = world["client"].get(f"/reports/day/{DAY}").json()
    # RPT-16 (operator ruling 2026-07-10): entry credit 355.12 - settlement
    # 369.00 = -13.88 net -- the honest broker-truth number, not the
    # fabricated 355.12 "win" a credit-only view would have shown.
    assert day["imported_cash"]["net"] == "-13.88"
    assert day["imported_cash"]["fees"] == "9.88"
    # The panel's broker-imported badge is driven off a non-empty
    # imported_fills list for the day (4 Trade legs + 4 settlement rows).
    assert len(day["imported_fills"]) == 8
    assert day["entries"] == [], "no recorded entry-level intent for an imported day"


@then("it appears in no Sharpe, expectancy, streak, outcome, targeting, or slippage figure")
def _(world):
    summary = world["client"].get("/reports/summary?period=all").json()
    metrics = summary["metrics"]
    # Only the REAL 2026-07-08 entry counts toward the quality-metric sample
    # -- the imported day (RPT-16 rule 3: no recorded entry-level intent) is
    # excluded from the Sharpe/expectancy/streak input series, even though
    # both days are in scope.
    assert metrics["status"] == "ok"
    assert metrics["sample_days"] == 1, \
        "the imported day must not inflate the Sharpe/streak sample"
    assert metrics["longest_losing_streak_days"] == 0, \
        "the imported day's -13.88 loss must not appear in the streak figure"

    # Outcome taxonomy ("targeting" TPF/TPT included) is entry-derived only
    # -- the imported day contributes no CondorFilled/entry, so it produces
    # no outcome row at all.
    taxonomy = summary["taxonomy"]["distribution"]
    assert taxonomy == {"FULL_EXPIRY": 1}, \
        "only the real entry's outcome appears; the imported day adds none"

    # Day-level slippage families are folded from ShortStopped events on that
    # day only -- an imported-only day journals none, so its stop-out
    # slippage sample is empty (n=0), never fabricated from imported cash.
    day = world["client"].get(f"/reports/day/{DAY}").json()
    assert day["slippage"]["stop_outs"]["n"] == 0
    assert day["slippage"]["stop_outs"]["mean"] is None


@then("it counts as a trading day in period buckets")
def _(world):
    summary = world["client"].get("/reports/summary?period=all").json()
    # RPT-01/RPT-16: the imported day counts as a trading day for period
    # bucketing even though it was never armed/attempted by this process --
    # both the real day and the imported day appear.
    assert DAY in summary["period_days"]
    assert "2026-07-08" in summary["period_days"]
    # And its broker-truth cash IS folded into the headline net (unlike the
    # quality-metric sample above, which deliberately excludes it).
    assert summary["core"]["imported_days"] == 1
    assert summary["core"]["imported_net"] == "-13.88"


# --- Scenario: Transaction-level idempotency ---------------------------------------

@given("a day imported fills-only and then re-imported")
def _(world):
    events: list = []
    broker = FakeBrokerReads(_real_trade_legs(), [])   # settlements not posted yet
    client = _rpt10_client(events, broker)

    r1 = client.post(f"/reports/backfill/{DAY}", json={"order_ids": [ORDER_ID]}).json()
    assert r1 == {"result": "imported", "fills": 4, "skipped_foreign": 0,
                  "settlements": 0, "ambiguous_settlements": 0}
    imported_after_r1 = [e for e in events if isinstance(e, ExternalFillImported)]
    assert len(imported_after_r1) == 4

    # The settlements post later (real broker behaviour, RPT-16(5)) -- mutate
    # the SAME broker facade instance the router already holds a reference
    # to, exactly as a later poll of the real broker would surface new rows.
    broker._settlements = _real_settlements()

    r2 = client.post(f"/reports/backfill/{DAY}", json={"order_ids": [ORDER_ID]}).json()
    r3 = client.post(f"/reports/backfill/{DAY}", json={"order_ids": [ORDER_ID]}).json()

    world.update(events=events, client=client, r1=r1, r2=r2, r3=r3)


@then("exactly the missing settlement rows are added once and a third run is a true no-op")
def _(world):
    assert world["r2"] == {"result": "imported", "fills": 0, "skipped_foreign": 0,
                           "settlements": 4, "ambiguous_settlements": 0}
    assert world["r3"] == {"result": "imported", "fills": 0, "skipped_foreign": 0,
                           "settlements": 0, "ambiguous_settlements": 0}, \
        "a third run against a fully-imported day is a true no-op"

    imported = [e for e in world["events"] if isinstance(e, ExternalFillImported)]
    assert len(imported) == 8, "4 Trade legs + 4 settlement rows, added exactly once each"

    day = world["client"].get(f"/reports/day/{DAY}").json()
    assert len(day["imported_fills"]) == 8
    assert day["imported_cash"]["net"] == "-13.88"


# --- Scenario: Never CondorFilled, never foreign, never guessed --------------------

def _ambiguous_settlement_world():
    """A foreign order trading the SAME symbol (C7540) our matched order
    settles on -- OWN-03: the settlement's ownership is genuinely ambiguous
    from broker data alone, so it must be skipped and counted, never guessed
    (mirrors tests/application/test_backfill.py's own fixture for this)."""
    trade_legs = _real_trade_legs()
    c7540 = "SPXW  260709C07540000"
    foreign_same_symbol = FakeTransaction(
        order_id=999999999, symbol=c7540, action="Sell to Open", quantity=D("2"),
        price=D("2.10"), executed_at=datetime(2026, 7, 9, 19, 29, tzinfo=timezone.utc))
    events: list = []
    broker = FakeBrokerReads(trade_legs + [foreign_same_symbol], _real_settlements())
    client = _rpt10_client(events, broker)
    result = client.post(f"/reports/backfill/{DAY}", json={"order_ids": [ORDER_ID]}).json()
    return events, result


@then("imported rows are ExternalFillImported events only")
def _(world):
    events, result = _ambiguous_settlement_world()
    assert events, "the import must actually append something"
    assert all(isinstance(e, ExternalFillImported) for e in events), \
        "imported history is data, never CondorFilled -- there is no recorded intent to fake"
    assert not any(isinstance(e, CondorFilled) for e in events)
    world.update(events=events, result=result)


@then("only operator-listed order ids import; foreign fills never do")
def _(world):
    # The foreign order (999999999) is not in `order_ids` -- its Sell to Open
    # leg is counted skipped_foreign, never imported as an ExternalFillImported.
    assert world["result"]["fills"] == 4
    assert world["result"]["skipped_foreign"] == 1
    imported_order_ids = {e.order_id for e in world["events"]}
    assert "999999999" not in imported_order_ids
    assert imported_order_ids == {ORDER_ID}


@then("a settlement symbol shared with skipped-foreign fills is counted ambiguous_settlements and skipped")
def _(world):
    # C7540's cash-settled assignment is ambiguous (the foreign order also
    # traded that symbol the same day) -- skipped and counted, the three
    # unshared Expiration settlements still import normally.
    assert world["result"]["ambiguous_settlements"] == 1
    assert world["result"]["settlements"] == 3
    assert not any(e.action == "Cash Settled Assignment" for e in world["events"])
