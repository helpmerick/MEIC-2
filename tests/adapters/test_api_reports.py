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
    ExternalFillImported,
    LongSaleStarted,
    LongSold,
    ShortStopped,
    StopPlaced,
)

PANEL = "http://127.0.0.1"


def _client(events=None, *, reporting_config=None, backfill_broker_reads=None):
    state = PersistentState(InMemoryStateStore())
    events = events if events is not None else []
    app = create_app(state, events, panel_origin=PANEL, reporting_config=reporting_config,
                     backfill_broker_reads=backfill_broker_reads)
    return TestClient(app), state, events


def _imported(day="2026-07-09", *, order_id="482214732", action="Sell to Open",
             price="3.00", fee="1.42") -> ExternalFillImported:
    return ExternalFillImported(
        day=day, at=f"{day}T14:31:00-04:00", order_id=order_id,
        symbol="SPXW  260709P05600000", action=action, quantity=1,
        price=D(price) if price is not None else None,
        fee=D(fee) if fee is not None else None,
        imported_at="2026-07-10T09:00:00-04:00", source="tastytrade_history")


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


def test_long_recovery_row_realized_vs_mark_and_buffer():
    """RPT-07 long recovery (2026-07-11 operator ruling): one row per
    LongSold, mark-at-stop from LongSaleStarted's stamp, buffer in force
    from StopPlaced.markup -- diff/shortfall computed from journaled events
    only."""
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")),
        ShortStopped(entry_id="2026-07-09#1", side="PUT", fill=D("3.90"), slippage=D("0.10")),
        StopPlaced(entry_id="2026-07-09#1", side="PUT", trigger=D("3.80"), markup=D("0.10")),
        LongSaleStarted(entry_id="2026-07-09#1", side="PUT",
                        mark_bid=D("2.00"), mark_ask=D("2.30"), intrinsic=D("0")),
        LongSold(entry_id="2026-07-09#1", side="PUT", recovery=D("2.05")),
    ]
    client, _, _ = _client(events)
    body = client.get("/reports/day/2026-07-09").json()
    lr = body["slippage"]["long_recovery"]
    assert lr["n"] == 1
    row = lr["rows"][0]
    assert row["entry_id"] == "2026-07-09#1" and row["side"] == "PUT"
    assert row["mark_mid"] == "2.15"      # (2.00 + 2.30) / 2
    assert row["realized"] == "2.05"
    assert row["diff"] == "-0.10"         # realized - mark_mid
    assert row["markup"] == "0.10"
    assert row["shortfall"] == "-1.95"    # markup - realized
    assert row["nle_estimate"] is None    # NLE-06: never fabricated (see below)
    assert lr["mean"] == lr["p50"] == lr["p90"] == lr["max"] == "-0.10"
    assert lr["nle_estimate_captured"] is False


def test_long_recovery_pre_stamping_event_renders_honest_none_not_zero():
    """A LongSaleStarted/StopPlaced recorded before the 2026-07-11 stamping
    shipped carries no mark/markup at all -- replayed as None (event-store
    codec's absent-on-decode path), and every value derived from it (diff,
    shortfall) must ALSO be None, never a fabricated 0."""
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")),
        ShortStopped(entry_id="2026-07-09#1", side="CALL", fill=D("3.90"), slippage=D("0.10")),
        LongSaleStarted(entry_id="2026-07-09#1", side="CALL"),          # pre-stamping: no mark
        StopPlaced(entry_id="2026-07-09#1", side="CALL", trigger=D("3.80")),  # pre-markup
        LongSold(entry_id="2026-07-09#1", side="CALL", recovery=D("1.80")),
    ]
    client, _, _ = _client(events)
    body = client.get("/reports/day/2026-07-09").json()
    row = body["slippage"]["long_recovery"]["rows"][0]
    assert row["realized"] == "1.80"      # always known -- it IS the sale
    assert row["mark_mid"] is None
    assert row["diff"] is None
    assert row["markup"] is None
    assert row["shortfall"] is None
    assert body["slippage"]["long_recovery"]["mean"] is None  # no diffs to aggregate


def test_long_recovery_no_long_sales_this_day_is_empty_not_null():
    """No fabricated GapNote once the family HAS a real shape: an ordinary
    day with no LEX activity is an honest empty family, not None."""
    events = [CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00"))]
    client, _, _ = _client(events)
    body = client.get("/reports/day/2026-07-09").json()
    assert body["slippage"]["long_recovery"] == {
        "rows": [], "n": 0, "mean": None, "p50": None, "p90": None, "max": None,
        "nle_estimate_captured": False,
    }


def test_long_recovery_keys_rows_by_entry_and_side_not_crossed():
    """Whipsaw, both sides (mirrors TC-NLE-05's own scenario): each side's
    row must use ITS OWN StopPlaced/LongSaleStarted, never the other side's."""
    events = [
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")),
        StopPlaced(entry_id="2026-07-09#1", side="PUT", trigger=D("3.80"), markup=D("0.10")),
        LongSaleStarted(entry_id="2026-07-09#1", side="PUT",
                        mark_bid=D("2.00"), mark_ask=D("2.20"), intrinsic=D("0")),
        LongSold(entry_id="2026-07-09#1", side="PUT", recovery=D("2.05")),
        StopPlaced(entry_id="2026-07-09#1", side="CALL", trigger=D("3.70"), markup=D("0.25")),
        LongSaleStarted(entry_id="2026-07-09#1", side="CALL",
                        mark_bid=D("1.00"), mark_ask=D("1.20"), intrinsic=D("0")),
        LongSold(entry_id="2026-07-09#1", side="CALL", recovery=D("1.05")),
    ]
    client, _, _ = _client(events)
    body = client.get("/reports/day/2026-07-09").json()
    rows = {r["side"]: r for r in body["slippage"]["long_recovery"]["rows"]}
    assert rows["PUT"]["markup"] == "0.10" and rows["CALL"]["markup"] == "0.25"
    assert rows["PUT"]["mark_mid"] == "2.10" and rows["CALL"]["mark_mid"] == "1.10"


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
    assert lines[0] == "date,mode,net_pnl,trust,wins,losses"
    assert lines[1] == "2026-07-09,paper,400.00,bot-computed,1,0"


def test_csv_export_daily_table_counts_wins_and_losses_per_day():
    """RPT-09 calendar-heatmap hover: wins/losses mirror `entry_win_rate`'s
    own pnl>0/pnl<0 threshold (a winner and a stopped-out loser same day)."""
    events = [
        DayArmed(date="2026-07-09", entry_count=2),
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("4.00")),
        CondorFilled(entry_id="2026-07-09#2", net_credit=D("4.00")),
        ShortStopped(entry_id="2026-07-09#2", side="PUT", fill=D("8.50"), slippage=D("0")),
    ]
    client, _, _ = _client(events)
    r = client.get("/reports/csv?table=daily&period=all")
    lines = r.text.strip().splitlines()
    assert lines[1] == "2026-07-09,paper,-50.00,bot-computed,1,1"


def test_csv_export_daily_table_zero_fills_a_skip_only_day_honestly():
    """A trading day where every attempt was skipped has zero filled entries
    -- 0/0 is the honest count, not a fabrication (same as daily_net's $0.00)."""
    events = [EntrySkipped(date="2026-07-09", entry_number=1, reason="not_armed")]
    client, _, _ = _client(events)
    r = client.get("/reports/csv?table=daily&period=all")
    lines = r.text.strip().splitlines()
    assert lines[1] == "2026-07-09,paper,0,bot-computed,0,0"


def test_csv_export_daily_table_blanks_wins_losses_for_an_imported_only_day():
    """RPT-16: an imported-only day carries no recorded entry-level outcome
    -- wins/losses render blank (not applicable), never a fabricated 0/0 for
    a day that plainly moved real broker cash."""
    events = [_imported()]
    client, _, _ = _client(events)
    r = client.get("/reports/csv?table=daily&period=all")
    lines = r.text.strip().splitlines()
    date, mode, net_pnl, trust, wins, losses = lines[1].split(",")
    assert date == "2026-07-09"
    assert wins == "" and losses == ""


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


def test_summary_counts_an_imported_day_as_a_trading_day_and_labels_it():
    """RPT-01: an imported day counts as a trading day; RPT-16(4): its trust
    label counts it out separately rather than folding it into the N/M
    broker-confirmed count."""
    events = [DayArmed(date="2026-07-08", entry_count=1),
              CondorFilled(entry_id="2026-07-08#1", net_credit=D("4.00")),
              _imported("2026-07-09")]
    client, _, _ = _client(events)
    body = client.get("/reports/summary?period=all").json()
    assert body["period_days"] == ["2026-07-08", "2026-07-09"]
    assert body["trust"]["imported_days"] == 1
    assert "1 imported day" in body["trust"]["label"]


def test_imported_day_contributes_cash_to_core_but_not_to_filled_or_credit():
    """RPT-16 rule 3: cash-level net/fees flow into core totals; entry-based
    figures (filled count, total_credit, entry_win_rate) stay pure."""
    events = [_imported("2026-07-09", price="3.00", fee="1.42")]
    client, _, _ = _client(events)
    body = client.get("/reports/summary?period=all").json()
    core = body["core"]
    assert core["imported_days"] == 1
    assert core["imported_fills"] == 1
    # Sell to Open, price 3.00 x qty 1 x $100 multiplier => +300.00 gross credit;
    # `fee` is already a REAL-DOLLAR broker charge (never per-share, unlike the
    # domain event `fee` fields) -- no further multiplier: net = 300.00 - 1.42.
    assert core["imported_net"] == "298.58"
    assert core["net_pnl"] == "298.58"
    assert core["imported_fees"] == "1.42"
    assert core["fees"] == "1.42"
    assert core["filled"] == 0            # no fold entry -- never an "entry attempt"
    assert core["total_credit"] == "0"  # entries-only, untouched by imports


def test_imported_day_excluded_from_sharpe_metrics_daily_series():
    """RPT-16 rule 3: a series with an imported day yields the SAME Sharpe
    inputs as without it."""
    real_events = [DayArmed(date=f"2026-07-{d:02d}", entry_count=1) for d in range(1, 6)]
    cfg = ReportingConfig(capital_base=D("10000"))
    baseline_client, _, _ = _client(real_events, reporting_config=cfg)
    baseline = baseline_client.get("/reports/summary?period=all").json()["metrics"]

    with_import_client, _, _ = _client(real_events + [_imported("2026-07-20")], reporting_config=cfg)
    with_import = with_import_client.get("/reports/summary?period=all").json()["metrics"]

    assert with_import["sharpe"] == baseline["sharpe"]
    assert with_import["sample_days"] == baseline["sample_days"]


def test_day_drilldown_renders_imported_fills_badge_and_cash_no_404():
    events = [_imported("2026-07-09")]
    client, _, _ = _client(events)
    r = client.get("/reports/day/2026-07-09")
    assert r.status_code == 200
    body = r.json()
    assert body["trust"]["status"] == "broker-imported"
    assert body["entries"] == []
    assert len(body["imported_fills"]) == 1
    assert body["imported_fills"][0]["order_id"] == "482214732"
    assert body["imported_fills"][0]["action"] == "Sell to Open"
    assert body["imported_cash"]["net"] == "298.58"
    assert body["imported_cash"]["fees"] == "1.42"


def test_day_drilldown_imported_fills_scoped_to_the_requested_day_only():
    """The `.day` field, not `.date`, so periods.scope_events must key on it
    too -- otherwise an imported fill from another day would leak in."""
    events = [_imported("2026-07-08", order_id="1"), _imported("2026-07-09", order_id="2")]
    client, _, _ = _client(events)
    body = client.get("/reports/day/2026-07-08").json()
    assert len(body["imported_fills"]) == 1
    assert body["imported_fills"][0]["order_id"] == "1"


class _FakeBroker:
    def __init__(self, fills, settlements=None):
        self._fills = fills
        self._settlements = settlements if settlements is not None else []

    async def day_fills(self, day):
        return self._fills

    async def day_settlements(self, day):
        return self._settlements


class _FakeTransaction:
    def __init__(self, order_id, symbol="X", action="Sell to Open", quantity=D("1"),
                price=D("3.00"), transaction_sub_type=None, value=None, net_value=None):
        from datetime import datetime, timezone
        self.order_id = order_id
        self.symbol = symbol
        self.action = action
        self.quantity = quantity
        self.price = price
        self.executed_at = datetime(2026, 7, 9, 14, 30, tzinfo=timezone.utc)
        self.regulatory_fees = None
        self.clearing_fees = None
        self.commission = None
        self.proprietary_index_option_fees = None
        self.transaction_sub_type = transaction_sub_type
        self.value = value
        self.net_value = net_value


def test_backfill_requires_auth_like_any_other_mutating_command():
    """NFR-06: a foreign Origin is rejected even for this endpoint, exactly
    like every other mutating command."""
    client, _, _ = _client(backfill_broker_reads=_FakeBroker([]))
    r = client.post("/reports/backfill/2026-07-09", json={"order_ids": ["1"]},
                    headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_backfill_happy_path_imports_only_supplied_order_ids():
    fills = [_FakeTransaction(order_id=1), _FakeTransaction(order_id=2)]
    client, _, events = _client(backfill_broker_reads=_FakeBroker(fills))
    r = client.post("/reports/backfill/2026-07-09", json={"order_ids": [1]})
    assert r.status_code == 200
    body = r.json()
    assert body == {"result": "imported", "fills": 1, "skipped_foreign": 1,
                    "settlements": 0, "ambiguous_settlements": 0}
    assert len([e for e in events if isinstance(e, ExternalFillImported)]) == 1


def test_backfill_reimport_appends_nothing_transaction_level_idempotency():
    """RPT-16(5), REWORKED per operator ruling 2026-07-10: identity is
    transaction-level, so a re-run re-fetches but appends nothing new --
    never a duplicate."""
    client, _, events = _client(backfill_broker_reads=_FakeBroker([_FakeTransaction(order_id=1)]))
    r1 = client.post("/reports/backfill/2026-07-09", json={"order_ids": [1]})
    assert r1.json()["fills"] == 1
    r2 = client.post("/reports/backfill/2026-07-09", json={"order_ids": [1]})
    assert r2.json() == {"result": "imported", "fills": 0, "skipped_foreign": 0,
                         "settlements": 0, "ambiguous_settlements": 0}
    assert len([e for e in events if isinstance(e, ExternalFillImported)]) == 1


def test_backfill_endpoint_round_trip_settlement_makes_the_day_a_loss():
    """RPT-16 settlement import (operator ruling 2026-07-10), end to end:
    import the real-shaped 2026-07-09 order + its Receive-Deliver rows, then
    read the day back -- the drill-down must show net -13.88 (a loss), never
    the +355.12 entry-credit-only view, and render the settlement rows with
    their sub_type action and signed value."""
    def leg(symbol, action, price):
        t = _FakeTransaction(order_id=482390058, symbol=symbol, action=action, price=price)
        t.regulatory_fees = D("-0.04")
        t.clearing_fees = D("-0.10")
        t.commission = D("-1.00")
        t.proprietary_index_option_fees = D("-0.08")  # 1.22/leg -> 4.88 total
        return t

    fills = [leg("SPXW  260709P07535000", "Sell to Open", D("2.20")),
             leg("SPXW  260709P07510000", "Buy to Open", D("0.40")),
             leg("SPXW  260709C07540000", "Sell to Open", D("2.15")),
             leg("SPXW  260709C07565000", "Buy to Open", D("0.35"))]
    settlements = [
        _FakeTransaction(order_id=None, symbol="SPXW  260709C07540000", action=None,
                         price=D("7540.0"), transaction_sub_type="Cash Settled Assignment",
                         value=D("-364.0"), net_value=D("-369.0")),
        _FakeTransaction(order_id=None, symbol="SPXW  260709P07535000", action=None,
                         price=None, transaction_sub_type="Expiration",
                         value=D("0"), net_value=D("0")),
        _FakeTransaction(order_id=None, symbol="SPXW  260709P07510000", action=None,
                         price=None, transaction_sub_type="Expiration",
                         value=D("0"), net_value=D("0")),
        _FakeTransaction(order_id=None, symbol="SPXW  260709C07565000", action=None,
                         price=None, transaction_sub_type="Expiration",
                         value=D("0"), net_value=D("0")),
    ]
    client, _, events = _client(backfill_broker_reads=_FakeBroker(fills, settlements))
    r = client.post("/reports/backfill/2026-07-09", json={"order_ids": [482390058]})
    assert r.json() == {"result": "imported", "fills": 4, "skipped_foreign": 0,
                        "settlements": 4, "ambiguous_settlements": 0}

    body = client.get("/reports/day/2026-07-09").json()
    assert body["imported_cash"] == {"net": "-13.88", "fees": "9.88"}
    rows = body["imported_fills"]
    assert len(rows) == 8
    cash_row = next(f for f in rows if f["action"] == "Cash Settled Assignment")
    assert cash_row["value"] == "-369.0"
    assert cash_row["fee"] == "5.0"
    assert cash_row["price"] == "7540.0"
    trade_row = next(f for f in rows if f["action"] == "Sell to Open")
    assert trade_row["value"] is None  # Trade-style rows carry no settlement value

    core = client.get("/reports/summary?period=all").json()["core"]
    assert core["imported_net"] == "-13.88"
    assert core["net_pnl"] == "-13.88"


def test_backfill_bad_day_format_is_400():
    client, _, _ = _client(backfill_broker_reads=_FakeBroker([]))
    r = client.post("/reports/backfill/not-a-day", json={"order_ids": ["1"]})
    assert r.status_code == 400


def test_backfill_without_a_broker_facade_is_400():
    """paper/no-broker composition roots pass no facade -- the endpoint must
    fail closed, never silently no-op or reach for a broker that isn't there."""
    client, _, _ = _client()  # no backfill_broker_reads
    r = client.post("/reports/backfill/2026-07-09", json={"order_ids": ["1"]})
    assert r.status_code == 400


def test_backfill_requires_order_ids():
    client, _, _ = _client(backfill_broker_reads=_FakeBroker([]))
    r = client.post("/reports/backfill/2026-07-09", json={"order_ids": []})
    assert r.status_code == 400


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
