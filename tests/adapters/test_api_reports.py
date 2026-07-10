"""RPT-10 — the read-only /reports/* API (doc 10). GETs are origin-open
exactly like the existing read model; every payload carries `mode` and the
UI-25 trust block; money is Decimal-exact strings. Uses the in-process
TestClient, same convention as tests/adapters/test_api.py.
"""
from decimal import Decimal as D

from fastapi.testclient import TestClient

from meic.adapters.api.app import create_app
from meic.adapters.api.reports import ReportingConfig
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.domain.events import (
    CondorFilled,
    CorrectionRecord,
    DayArmed,
    DayBrokerConfirmed,
    EntryClosed,
    EntryMarkSample,
    EntrySkipped,
    ShortStopped,
)

PANEL = "http://127.0.0.1"


def _client(events=None, *, reporting_config=None):
    state = PersistentState(InMemoryStateStore())
    events = events if events is not None else []
    app = create_app(state, events, panel_origin=PANEL, reporting_config=reporting_config)
    return TestClient(app), state, events


def test_reports_endpoints_require_no_origin_get_is_open():
    """RPT-10: GETs are origin-open like every other read model -- no
    origin header required (the security middleware only gates mutating
    verbs)."""
    client, _, _ = _client()
    assert client.get("/reports/summary").status_code == 200


def test_summary_shape_and_mode_and_trust_block():
    events = [DayArmed(date="2026-07-09", entry_count=1),
              CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"))]
    client, _, _ = _client(events)
    r = client.get("/reports/summary?period=all")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "paper"
    assert set(body) >= {"mode", "period_days", "trust", "core", "metrics",
                         "taxonomy", "health", "waterfall"}
    assert body["core"]["net_pnl"] == "400.00"
    assert body["trust"]["status"] == "bot-computed"


def test_summary_metrics_render_unconfigured_without_a_capital_base():
    client, _, _ = _client(reporting_config=ReportingConfig(capital_base=None))
    body = client.get("/reports/summary?period=all").json()
    assert body["metrics"] == {"status": "unconfigured"}


def test_summary_metrics_render_when_capital_base_is_configured():
    events = [DayArmed(date=f"2026-07-{d:02d}", entry_count=1) for d in range(1, 6)]
    client, _, _ = _client(events, reporting_config=ReportingConfig(capital_base=D("10000")))
    body = client.get("/reports/summary?period=all").json()
    assert body["metrics"]["status"] == "ok"
    assert body["metrics"]["roc"] is not None


def test_summary_period_day_filters_to_a_single_day():
    events = [
        DayArmed(date="2026-07-08", entry_count=1),
        CondorFilled(entry_id="2026-07-08#1", net_credit=D("4.00")),
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("2.00")),
    ]
    client, _, _ = _client(events)
    body = client.get("/reports/summary?day=2026-07-09").json()
    assert body["period_days"] == ["2026-07-09"]
    assert body["core"]["net_pnl"] == "200.00"


def test_summary_trust_reflects_broker_confirmed_days():
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")),
        DayBrokerConfirmed(date="2026-07-09", at="2026-07-09T16:20:00-04:00"),
    ]
    client, _, _ = _client(events)
    body = client.get("/reports/summary?day=2026-07-09").json()
    assert body["trust"]["status"] == "broker-confirmed"


def test_summary_health_counts_corrections_and_watchdog():
    from meic.domain.events import WatchdogEscalated

    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        EntrySkipped(date="2026-07-09", entry_number=2, reason="incomplete_chain"),
        WatchdogEscalated(entry_id="2026-07-09#1", side="PUT", mark_at_breach=D("3.85"),
                          elapsed_seconds=D("20"), fill_price=D("3.90")),
        CorrectionRecord(date="2026-07-09", field="fees", bot_value="1", broker_value="2",
                         diff="1", at="t"),
    ]
    client, _, _ = _client(events)
    body = client.get("/reports/summary?day=2026-07-09").json()
    assert body["health"]["skip_reason_histogram"] == {"incomplete_chain": 1}
    assert body["health"]["watchdog_escalations"] == 1
    assert body["health"]["correction_count"] == 1


def test_day_drilldown_shape_entries_timeline_corrections():
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")),
        ShortStopped(entry_id="2026-07-09#1", side="PUT", fill=D("3.90"),
                    slippage=D("0.10")),
        EntryMarkSample(entry_id="2026-07-09#1", at="t", spot=D("5650")),
        CorrectionRecord(date="2026-07-09", field="fees", bot_value="0",
                         broker_value="20.00", diff="20.00", at="t"),
    ]
    client, _, _ = _client(events)
    r = client.get("/reports/day/2026-07-09")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-07-09"
    assert len(body["entries"]) == 1
    assert body["entries"][0]["sides_stopped"] == ["PUT"]
    assert len(body["timeline"]["marks"]) == 1
    assert body["slippage"]["stop_outs"]["n"] == 1
    assert body["slippage"]["stop_outs"]["mean"] == "0.10"
    assert len(body["corrections"]) == 1
    assert body["corrections"][0]["broker_value"] == "20.00"


def test_day_drilldown_404_for_a_day_with_no_data():
    client, _, _ = _client([])
    r = client.get("/reports/day/2026-07-01")
    assert r.status_code == 404


def test_close_initiator_marks_appear_on_the_timeline():
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")),
        EntryClosed(entry_id="2026-07-09#1", initiator="eod"),
    ]
    client, _, _ = _client(events)
    body = client.get("/reports/day/2026-07-09").json()
    types = {m["type"] for m in body["timeline"]["markers"]}
    assert "CondorFilled" in types and "EntryClosed" in types


def test_csv_export_daily_table():
    events = [DayArmed(date="2026-07-09", entry_count=1),
              CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"))]
    client, _, _ = _client(events)
    r = client.get("/reports/csv?table=daily&period=all")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0] == "date,mode,net_pnl,trust"
    assert lines[1] == "2026-07-09,paper,400.00,bot-computed"


def test_csv_export_entries_table():
    events = [CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"))]
    client, _, _ = _client(events)
    r = client.get("/reports/csv?table=entries&period=all")
    assert "2026-07-09#1" in r.text


def test_csv_export_corrections_table():
    # A CorrectionRecord only ever exists for a day that already qualifies as
    # a trading day (RPT-15 only reconciles days with real activity) --
    # DayArmed here mirrors that reality so the day is in the "all" scope.
    events = [
        DayArmed(date="2026-07-09", entry_count=1),
        CorrectionRecord(date="2026-07-09", field="fees", bot_value="0",
                         broker_value="20.00", diff="20.00", at="t"),
    ]
    client, _, _ = _client(events)
    r = client.get("/reports/csv?table=corrections&period=all")
    lines = r.text.strip().splitlines()
    assert lines[0] == "date,mode,field,bot_value,broker_value,diff,at"
    assert "20.00" in r.text


def test_csv_export_rejects_an_unknown_table():
    client, _, _ = _client()
    r = client.get("/reports/csv?table=nonsense&period=all")
    assert r.status_code == 422


def test_paper_and_live_events_are_never_commingled():
    """Principle 3: mode never commingles -- each app instance folds ONLY its
    own composition's events list; a second, separate list with the SAME
    entry id and a wildly different credit never leaks in."""
    live_events = [CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"))]
    paper_events = [CondorFilled(entry_id="2026-07-09#1", net_credit=D("9999.00"))]
    live_client, _, _ = _client(live_events)
    paper_client, _, _ = _client(paper_events)
    live_body = live_client.get("/reports/summary?period=all").json()
    paper_body = paper_client.get("/reports/summary?period=all").json()
    assert live_body["core"]["net_pnl"] == "400.00"
    assert paper_body["core"]["net_pnl"] == "999900.00"
