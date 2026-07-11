"""Hand-written step definitions for TC-UI-07 — UI-28 contract-dollar display,
UI-18a per-row markup disclosure, UI-26a heatmap honesty, UI-23a local label.

BINDING STRATEGY (same split every UI-flavoured TC here uses): the backend
halves bind to the REAL folds/validation/API payloads in Python
(reporting/folds.py is UI-28's ONE aggregation path; domain/schedule.py +
ScheduleService is UI-18a's reject-never-clamp authority; /reports payloads
carry Decimal-exact strings); the TypeScript display halves (money.ts's exact
BigInt digit-shift, SchedulePanel's disclosure line, CalendarHeatmap's hover
honesty, SlippagePanels' ticks-and-dollars columns) execute through the REAL
vitest suites via the session-scoped `vitest_ui07_result` fixture
(tests/bdd/conftest.py) — never a Python re-implementation.
"""
from decimal import Decimal as D
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, scenarios, then

from meic.adapters.api.app import create_app
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.application.schedule_service import ScheduleService
from meic.domain.events import (
    CondorFilled,
    DayArmed,
    FilledLeg,
    LongSaleStarted,
    LongSold,
    ShortStopped,
    StopPlaced,
)
from meic.domain.stop_policy import markup_worst_case_increase
from meic.reporting.folds import (
    core_results,
    daily_net,
    entries_by_day,
    entry_credit_dollars,
    entry_dollars,
)

scenarios("../features/TC-UI-07.feature")

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

PUT_SHORT, PUT_LONG = "SPXW  260709P07525000", "SPXW  260709P07505000"
CALL_SHORT, CALL_LONG = "SPXW  260709C07550000", "SPXW  260709C07570000"


def _legs(qty: int):
    return (
        FilledLeg(symbol=PUT_LONG, right="P", role="long", qty=qty, price=D("0.10")),
        FilledLeg(symbol=PUT_SHORT, right="P", role="short", qty=qty, price=D("1.60")),
        FilledLeg(symbol=CALL_SHORT, right="C", role="short", qty=qty, price=D("2.60")),
        FilledLeg(symbol=CALL_LONG, right="C", role="long", qty=qty, price=D("0.10")),
    )


def _client(events):
    state = PersistentState(InMemoryStateStore())
    app = create_app(state, events, panel_origin="http://127.0.0.1")
    return TestClient(app)


@pytest.fixture
def world():
    return {}


# --- Scenario: Entry money renders as position dollars with one consistency ------

@given("an entry with contracts = 2 and per-contract net credit 4.00")
def _(world):
    world["events"] = [
        DayArmed(date="2026-07-09", entry_count=2),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"), legs=_legs(2)),
        # a second, 1-contract entry so the aggregate below has to actually
        # sum per-entry dollars (not multiply once).
        CondorFilled(entry_id="2026-07-09#2", net_credit=D("3.50"), legs=_legs(1)),
    ]


@then("displays show 800 dollars and side displays sum exactly to the total")
def _(world, vitest_ui07_result):
    # Backend: THE dollarization (UI-28: per-contract value x 100 x contracts)
    # — 4.00 x 100 x 2 = $800, Decimal-exact.
    entry = entries_by_day(world["events"])["2026-07-09"][0]
    assert entry_credit_dollars(entry) == D("800.00")

    # Frontend: money.ts performs the SAME conversion for display with exact
    # BigInt digit arithmetic, and the worked side-split example pins the
    # consistency invariant (side displays sum exactly to the total).
    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "scales by contracts > 1 exactly" in output
    assert ("matches the worked example from the operator's request: "
            "a $5.20 credit split $2.24/$2.96 across two sides") in output


@then("aggregates sum per-entry dollars via the single aggregation path")
def _(world, vitest_ui07_result):
    # ONE aggregation path (reporting/folds.py): the period total is the SUM
    # of per-entry dollars — 800 + 350 — never re-derived from averages or
    # recomputed per view.
    entries = entries_by_day(world["events"])["2026-07-09"]
    per_entry = [entry_credit_dollars(e) for e in entries]
    assert per_entry == [D("800.00"), D("350.00")]
    assert core_results(world["events"]).total_credit == D("1150.00")
    assert daily_net(world["events"])["2026-07-09"] == sum(
        (entry_dollars(e) for e in entries), D("0"))

    # Frontend aggregates go through the same shape: exact per-entry scaling
    # first (contractDollarsValue), one summation, one formatter.
    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "returns the exact scaled number" in output
    assert "cleans float summation noise" in output


# --- Scenario: Exemptions stay native ---------------------------------------------

@then("quoted prices, ticks, and trigger prices render per-share")
def _(world, vitest_ui07_result):
    # Backend payloads keep the deliberately-native figures per-share: the
    # long-recovery rows' mark/realized/buffer are per-share price strings
    # (UI-28 EXEMPT list), exactly as journaled.
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"), legs=_legs(1)),
        StopPlaced(entry_id="2026-07-09#1", side="PUT", trigger=D("3.80"), markup=D("0.10")),
        ShortStopped(entry_id="2026-07-09#1", side="PUT", fill=D("3.90"), slippage=D("0.10")),
        LongSaleStarted(entry_id="2026-07-09#1", side="PUT",
                        mark_bid=D("2.00"), mark_ask=D("2.30"), intrinsic=D("0")),
        LongSold(entry_id="2026-07-09#1", side="PUT", recovery=D("2.05")),
    ]
    world["day_payload"] = _client(events).get("/reports/day/2026-07-09").json()
    row = world["day_payload"]["slippage"]["long_recovery"]["rows"][0]
    assert row["mark_mid"] == "2.15" and row["realized"] == "2.05"   # per-share
    assert row["markup"] == "0.10"                                    # per-share

    # Frontend: the per-share columns render with plain money() (never x100) —
    # pinned by the real SlippagePanels suite.
    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "renders per-share mark/realized/buffer and contract-dollar diff/shortfall" in output


@then("slippage renders in both ticks and position dollars")
def _(world, vitest_ui07_result):
    # Backend: BOTH families carry a ticks aggregate beside the per-share
    # dollar figures (stop-outs since RPT-07 shipped; long recovery per
    # UI-28 v1.61, derived with the same EC-STP-03 tick 0.05).
    slippage = world["day_payload"]["slippage"]
    assert slippage["stop_outs"]["mean"] == "0.10"        # per-share dollars
    assert slippage["stop_outs"]["mean_ticks"] == "2"     # 0.10 / 0.05
    assert slippage["long_recovery"]["mean"] == "-0.10"
    assert slippage["long_recovery"]["mean_ticks"] == "-2"

    # Frontend: dollars render as x100 position cash, ticks stay ticks.
    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "renders per-share slippage figures as ×100 cash" in output


@then("no displayed cash number passes through binary float")
def _(world, vitest_ui07_result):
    # Backend: every money field in the payload is a Decimal-exact STRING
    # (reports.py `_s`/`str`), never a JSON float.
    row = world["day_payload"]["slippage"]["long_recovery"]["rows"][0]
    for key in ("mark_mid", "realized", "diff", "markup", "shortfall"):
        assert row[key] is None or isinstance(row[key], str), \
            f"{key} must be a Decimal string or null, never a float: {row[key]!r}"
    for key in ("mean", "p50", "p90", "max", "mean_ticks"):
        v = world["day_payload"]["slippage"]["stop_outs"][key]
        assert v is None or isinstance(v, str)

    # Frontend: the decimal shift is exact BigInt digit arithmetic — the
    # classic float-multiply corruption case is pinned green.
    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "shifts a small premium exactly (the classic float-multiply failure case)" in output
    assert "preserves sub-cent precision in the source Decimal rather than rounding it away" in output


# --- Scenario: Markup dial discloses per row ---------------------------------------

@given("a schedule row sets stop_rebate_markup 0.50 with contracts 2")
def _(world):
    world["row"] = {"time": "10:00", "stop_rebate_markup": "0.50", "contracts": 2}
    world["svc"] = ScheduleService(PersistentState(InMemoryStateStore()))


@then('the row shows the shortfall sentence AND "worst case rises by $200" (0.50 x 100 x 2 x 2)')
def _(world, vitest_ui07_result):
    # Backend: the ratified formula (UI-18a: markup x 100 x contracts x 2 —
    # both stops carry the markup) — 0.50 x 100 x 2 x 2 = $200, and the row
    # itself saves cleanly (a legal $0.05-step value).
    assert markup_worst_case_increase(D("0.50"), contracts=2) == D("200")
    out = world["svc"].save([world["row"]])
    assert out["result"] == "saved"
    assert out["rows"][0]["stop_rebate_markup"] == "0.50"

    # Frontend: the per-row hint carries BOTH disclosures — the UI-18
    # shortfall sentence AND the worst-case dollar figure (money.ts mirrors
    # markup_worst_case_increase exactly, exact BigInt arithmetic).
    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "discloses the UI-18 shortfall sentence alongside the dollar figure" in output
    assert "mirrors domain/stop_policy.py's markup_worst_case_increase" in output
    assert "markup 0.30, 2 contracts: +$120" in output   # the x100 x contracts x2 shape


@then("out-of-grid values are rejected, never clamped")
def _(world, vitest_ui07_result):
    svc = world["svc"]
    # Off-step: rejected per-row with the precise reason — and NOT saved.
    bad_step = svc.save([{**world["row"], "stop_rebate_markup": "0.52"}])
    assert bad_step["result"] == "invalid"
    assert {"field": "stop_rebate_markup", "reason": "bad_step", "index": 0} in bad_step["errors"]
    # Out of range: rejected, never clamped to the 5.00 edge.
    out_of_range = svc.save([{**world["row"], "stop_rebate_markup": "5.05"}])
    assert out_of_range["result"] == "invalid"
    assert {"field": "stop_rebate_markup", "reason": "out_of_range", "index": 0} in out_of_range["errors"]

    # Frontend: client-side validation REJECTS (outlines the cell) — it never
    # clamps; the backend above stays authoritative.
    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "rejects a step that isn't a multiple of $0.05" in output
    assert "rejects out-of-range values" in output
    assert "rejects an invalid step client-side (reject, never clamp) and shows the range/step hint" in output


# --- Scenario: Heatmap honesty ------------------------------------------------------

@given("an imported day and a day with no data")
def _(world):
    from meic.domain.events import ExternalFillImported

    world["events"] = [
        # RPT-16: a broker-imported day — real cash, no recorded entry intent.
        ExternalFillImported(
            day="2026-07-08", at="2026-07-08T14:31:00-04:00", order_id="482214732",
            symbol=PUT_SHORT, action="Sell to Open", quantity=1, price=D("3.00"),
            fee=D("1.42"), imported_at="2026-07-10T09:00:00-04:00",
            source="tastytrade_history"),
        # …and a normal bot day beside it. 2026-07-07 has NO data at all.
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"), legs=_legs(1)),
    ]
    world["csv"] = _client(world["events"]).get(
        "/reports/csv?table=daily&period=all").text.strip().splitlines()


@then('the imported day shows its imported values and the empty day shows "no data"')
def _(world, vitest_ui07_result):
    header, *rows = world["csv"]
    assert header == "date,mode,net_pnl,trust,wins,losses,entries"
    by_date = {r.split(",")[0]: r.split(",") for r in rows}
    # The imported day shows its IMPORTED broker cash (3.00 x 100 - 1.42).
    assert by_date["2026-07-08"][2] == "298.58"
    # The day with no data is simply ABSENT from the series — the heatmap
    # renders it as an honest "no trading day" cell, never a fabricated row.
    assert "2026-07-07" not in by_date

    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "renders empty state honestly when there are no trading days at all" in output


@then("a fabricated 0-0 never renders")
def _(world, vitest_ui07_result):
    by_date = {r.split(",")[0]: r.split(",") for r in world["csv"][1:]}
    # RPT-16: the imported day's wins/losses/entries are BLANK (null), never a
    # fabricated 0/0 for a day that plainly moved real broker cash.
    assert by_date["2026-07-08"][4:7] == ["", "", ""]
    # The real bot day keeps its honest fold-derived counts.
    assert by_date["2026-07-09"][4:7] == ["1", "0", "1"]

    # Frontend: the hover box says "not applicable (broker-imported)" and the
    # tooltip literally never contains "0 wins"/"0 entries" for that day.
    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "never fabricates counts for an imported day" in output
    # UI-26a: the hover box shows date, net $, ENTRIES, wins/losses.
    assert "shows a styled box with date, entries, wins/losses, and signed P&L" in output


@then("weekends render visually distinct from zero-P&L trading days")
def _(world, vitest_ui07_result):
    rc, output = vitest_ui07_result
    assert rc == 0, output
    assert "greys out Saturday/Sunday as a distinct weekend treatment, never conflated with idle" in output
    assert "a zero-P&L trading day renders as a flat trading cell, visually distinct from a weekend" in output


# --- Scenario: The local label is the browser's zone, not geolocation ---------------
#
# UI-23a label wording divergence escalated 2026-07-11: the operator ruled the
# shipped echo label reads "local", while the ratified UI-23a text says the
# resolved city (e.g. "London"). Pending that ruling, these steps pin ONLY the
# invariants BOTH versions share — the conversion zone comes exclusively from
# `Intl.DateTimeFormat().resolvedOptions().timeZone`, and no geolocation /
# location lookup exists anywhere in the frontend source. The shipped "local"
# wording is deliberately NOT asserted against the ratified city wording here.

@then("the echo label names the Intl-resolved zone and no location lookup ever occurs")
def _(world, vitest_ui07_result):
    src_files = [p for p in FRONTEND_SRC.rglob("*")
                 if p.suffix in (".ts", ".tsx") and ".test." not in p.name]
    assert src_files, "frontend source tree not found"
    joined = "\n".join(p.read_text(encoding="utf-8") for p in src_files)

    # The ONE zone source both label wordings share: the browser's own Intl
    # setting — resolved in exactly one place (time.ts localZone()).
    assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in joined
    time_ts = (FRONTEND_SRC / "time.ts").read_text(encoding="utf-8")
    assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in time_ts

    # No geolocation / location lookup of ANY kind, anywhere in the frontend:
    # nothing is looked up, requested, or tracked (UI-23a's shared clause).
    for forbidden in ("geolocation", "getCurrentPosition", "watchPosition",
                      "ipinfo", "ip-api", "geoip", "ipgeolocation"):
        assert forbidden.lower() not in joined.lower(), \
            f"location lookup found in frontend source: {forbidden}"

    # And the Intl-resolved zone is what the label machinery names (zoneLabel
    # resolves the IANA zone to its city — the ratified wording's mechanism,
    # exercised green regardless of which label wording ultimately ships).
    rc, output = vitest_ui07_result
    assert rc == 0, output
