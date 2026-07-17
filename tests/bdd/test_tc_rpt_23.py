"""TC-RPT-23 -- RPT-17/UI-33 (v1.82) day-trades table + Timing & Unmanaged
report, and the D8b sampler extension. Backend halves call the real
implementation directly (reporting/day_table.py, server.py's
`_sample_marks_once`, and the `/reports/day-table` endpoint via TestClient);
UI-flavoured clauses additionally bind their frontend half through the
session-scoped `vitest_rpt23_result` fixture (tests/bdd/conftest.py), the
same dual-half strategy TC-DAY-07/TC-DOC-01 already use.
"""
import inspect
from datetime import date, datetime, timezone
from decimal import Decimal as D

from fastapi.testclient import TestClient
from pytest_bdd import given, scenario, then

from meic.adapters.api.app import create_app
from meic.adapters.api.server import _sample_marks_once
from meic.adapters.dxlink.chain_snapshot import ChainSnapshot
from meic.adapters.occ import occ_symbol
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.domain.chain import ChainSide, Mark
from meic.domain.events import CondorFilled, DayArmed, EntryClosed, EntryMarkSample, FilledLeg, SideExpired
from meic.domain.projection import fold
from meic.reporting import day_table as dt
from meic.reporting.folds import core_results, entry_credit_dollars, entry_dollars
from meic.reporting.periods import scope_events

PANEL = "http://127.0.0.1"
EXP = date(2026, 7, 9)


@scenario("../features/TC-RPT-23.feature", "The table shows today's entries from the one aggregation path")
def test_the_table_shows_todays_entries_from_the_one_aggregation_path():
    pass


@scenario("../features/TC-RPT-23.feature", "Unmanaged P&L is computed from recorded samples only")
def test_unmanaged_pnl_is_computed_from_recorded_samples_only():
    pass


@scenario("../features/TC-RPT-23.feature", "Sampling continues after close, day-scoped (D8b)")
def test_sampling_continues_after_close_day_scoped_d8b():
    pass


@scenario("../features/TC-RPT-23.feature", "Provisional stays provisional")
def test_provisional_stays_provisional():
    pass


def _leg(right, role, strike, qty=1, price=D("1.00")):
    return FilledLeg(symbol=occ_symbol("SPXW", EXP, right, D(strike)), right=right,
                      role=role, qty=qty, price=price)


FULL_LEGS = (
    _leg("P", "short", "7535"), _leg("P", "long", "7510"),
    _leg("C", "short", "7540"), _leg("C", "long", "7565"),
)


def _client(events):
    state = PersistentState(InMemoryStateStore())
    app = create_app(state, events, panel_origin=PANEL)
    return TestClient(app)


# --- Scenario 1: the one aggregation path --------------------------------------

@given("two closed entries and one open entry today", target_fixture="table_vector")
def _():
    events = [
        DayArmed(date="2026-07-09", entry_count=3),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), fee=D("4.88"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
        CondorFilled(entry_id="2026-07-09#2", net_credit=D("3.40"), fee=D("4.88"), legs=FULL_LEGS,
                    at="2026-07-09T10:00:00+00:00"),
        EntryClosed(entry_id="2026-07-09#2", initiator="manual", at="2026-07-09T11:00:00+00:00"),
        CondorFilled(entry_id="2026-07-09#3", net_credit=D("3.20"), legs=FULL_LEGS,
                    at="2026-07-09T11:30:00+00:00"),
    ]
    client = _client(events)
    body = client.get("/reports/day-table").json()
    return {"events": events, "body": body}


@then("the Trading tab's table shows all three with per-side badges, credits, and realized P&L net of fees")
def _(table_vector, vitest_rpt23_result):
    body = table_vector["body"]
    assert [r["entry_id"] for r in body["rows"]] == [
        "2026-07-09#1", "2026-07-09#2", "2026-07-09#3"]
    for row in body["rows"]:
        assert set(row["side_badges"]) == {"PUT", "CALL"}
        assert row["net_credit"] is not None
    # The two CLOSED rows carry a real realized P&L; the still-open third row
    # has none here (no live enricher wired in this fixture -- see the next
    # Then clause for its own honest "unrealized" shape).
    assert body["rows"][0]["pnl"] is not None
    assert body["rows"][1]["pnl"] is not None

    rc, output = vitest_rpt23_result
    assert rc == 0, output
    assert ("shows per-side badges, net credit, and realized P&L net of fees "
            "for a closed entry" in output)


@then("the open row shows live P&L badged unrealized and updates in place")
def _(table_vector, vitest_rpt23_result):
    open_row = table_vector["body"]["rows"][2]
    assert open_row["entry_id"] == "2026-07-09#3"
    assert open_row["pnl_unrealized"] is True

    rc, output = vitest_rpt23_result
    assert rc == 0, output
    assert "badges an open row's live P&L as unrealized and updates it in place on the next poll" in output


@then("every figure matches the canonical aggregation byte-for-byte (no view-local recompute)")
def _(table_vector):
    events = table_vector["events"]
    day = fold(events)
    body = table_vector["body"]
    for row in body["rows"]:
        e = day.entries[row["entry_id"]]
        assert row["net_credit"] == str(entry_credit_dollars(e))
        if row["entry_id"] != "2026-07-09#3":  # the still-open row has no realized figure
            assert row["pnl"] == str(entry_dollars(e))

    totals = core_results(scope_events(events, ("2026-07-09",)))
    assert body["day_total"]["net_pnl"] == str(totals.net_pnl)
    assert body["day_total"]["fees"] == str(totals.fees)
    assert body["day_total"]["total_credit"] == str(totals.total_credit)


# --- Scenario 2: Unmanaged P&L from recorded samples only ---------------------

@given("an entry closed at 10:00 whose legs were sampled through 16:00", target_fixture="unmanaged_vector")
def _():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
        EntryClosed(entry_id="2026-07-09#1", initiator="take_profit", at="2026-07-09T14:00:00+00:00"),
        # 20:00 UTC == 16:00 ET exactly -- the recorded close sample.
        EntryMarkSample(entry_id="2026-07-09#1", at="2026-07-09T20:00:00+00:00",
                        put_short_mid=D("0.50"), put_long_mid=D("0.05"),
                        call_short_mid=D("0.40"), call_long_mid=D("0.03")),
    ]
    # A second entry, otherwise identical, with NO recorded sample at all.
    events_no_sample = [
        CondorFilled(entry_id="2026-07-09#2", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
        EntryClosed(entry_id="2026-07-09#2", initiator="take_profit", at="2026-07-09T14:00:00+00:00"),
    ]
    all_events = events + events_no_sample
    day = fold(all_events)
    return {
        "sampled": (day.entries["2026-07-09#1"], all_events),
        "unsampled": (day.entries["2026-07-09#2"], all_events),
    }


@then("its Unmanaged P&L = premium received minus the recorded 16:00 spread value")
def _(unmanaged_vector):
    entry, events = unmanaged_vector["sampled"]
    result = dt.unmanaged_pnl(entry, events)
    assert result.status == "ok"
    # premium received = 3.60 x 100 = $360; 16:00 spread = (0.50-0.05)+(0.40-0.03) = 0.82 -> $82
    assert D(result.value) == D("360.00") - D("82.00")


@then('an entry with missing close-time samples renders "no data (not sampled)", never an interpolation')
def _(unmanaged_vector, vitest_rpt23_result):
    entry, events = unmanaged_vector["unsampled"]
    result = dt.unmanaged_pnl(entry, events)
    assert result.status == "no_data"
    assert result.value is None

    rc, output = vitest_rpt23_result
    assert rc == 0, output
    assert 'renders "no data (not sampled)" for a missing close-time sample, never an interpolation' in output


# --- Scenario 3: D8b sampler extension -----------------------------------------

def _snapshot(at):
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")), D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}
    call_marks = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")), D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}
    return ChainSnapshot(
        spot=D("7538"), expiration=EXP,
        put_side=ChainSide(strikes_toward_otm=(D("7535"), D("7510")), marks=put_marks),
        call_side=ChainSide(strikes_toward_otm=(D("7540"), D("7565")), marks=call_marks),
        put_band=(), call_band=(), symbols={}, taken_at=at, stale=False)


class _Comp:
    def __init__(self, events):
        self.events = list(events)


@given("an entry that closes mid-morning", target_fixture="d8b_vector")
def _():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS),
        EntryClosed(entry_id="2026-07-09#1", initiator="manual", at="2026-07-09T14:00:00+00:00"),  # 10:00 ET
    ]
    return {"events": events}


@then("its legs keep receiving 1-minute samples until 16:00 and none after")
def _(d8b_vector):
    comp = _Comp(d8b_vector["events"])
    # 11:00 ET (after the 10:00 ET close, before 16:00) -- still sampled.
    _sample_marks_once(comp, _snapshot(datetime(2026, 7, 9, 15, 0, tzinfo=timezone.utc)))
    # 16:00 ET exactly -- still sampled (the close instant itself).
    _sample_marks_once(comp, _snapshot(datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)))
    # 16:05 ET -- past the close, no more samples.
    _sample_marks_once(comp, _snapshot(datetime(2026, 7, 9, 20, 5, tzinfo=timezone.utc)))

    samples = [e for e in comp.events if isinstance(e, EntryMarkSample)]
    assert len(samples) == 2
    assert samples[0].at == "2026-07-09T15:00:00+00:00"
    assert samples[1].at == "2026-07-09T20:00:00+00:00"


@then("the counterfactual never triggers any fetch of historical quotes")
def _(d8b_vector):
    # `_sample_marks_once` takes ONLY (comp, snapshot) -- the ALREADY-HELD
    # live snapshot every open-entry sample reads (no broker/feed reference
    # at all) -- so it is structurally incapable of reaching out for a
    # historical quote; D8b rides this exact same signature unchanged.
    sig = inspect.signature(_sample_marks_once)
    assert list(sig.parameters) == ["comp", "snapshot"]

    comp = _Comp(d8b_vector["events"])
    _sample_marks_once(comp, _snapshot(datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)))
    assert all(isinstance(e, (CondorFilled, EntryClosed, EntryMarkSample)) for e in comp.events)


# --- Scenario 4: provisional stays provisional --------------------------------

@given("a row held to expiry whose broker settlement has not landed", target_fixture="provisional_vector")
def _():
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS,
                    at="2026-07-09T09:32:00+00:00"),
        SideExpired(entry_id="2026-07-09#1", side="PUT"),
        SideExpired(entry_id="2026-07-09#1", side="CALL"),
    ]
    return {"events": events}


@then("its realized P&L renders the EOD-01 PROVISIONAL label, never fake finality")
def _(provisional_vector):
    events = provisional_vector["events"]
    entry = fold(events).entries["2026-07-09#1"]
    assert dt.is_provisional(entry, events) is True

    client = _client(events)
    row = client.get("/reports/day-table").json()["rows"][0]
    assert row["provisional"] is True
