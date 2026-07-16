"""FastAPI control panel — NFR-06 security + UI contract (TC-NFR-06/UI-01/02).

Uses the in-process TestClient — no network. Prose-TC functions named
test_tc_* so the traceability checker counts them.
"""
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from meic.adapters.api.app import create_app
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.config.validation import ConfigRejected, validate_bind
from meic.domain.events import CondorFilled, DayArmed, FilledLeg

PANEL = "http://127.0.0.1"


def _client(*, api_token=None):
    state = PersistentState(InMemoryStateStore())
    events: list = []
    app = create_app(state, events, api_token=api_token, panel_origin=PANEL)
    return TestClient(app), state, events


# --- TC-NFR-06: control-panel security ---------------------------------------

def test_tc_nfr_06_foreign_origin_rejected_even_on_localhost():
    """TC-NFR-06: a mutating request with a foreign Origin is 403, even on
    localhost (a hostile page can fire at localhost from the browser)."""
    client, state, _ = _client()
    state.entry_schedule = [{"time": "10:00"}]
    # same-origin arm works
    assert client.post("/arm", headers={"origin": PANEL}).status_code == 200
    # foreign-origin arm is refused
    r = client.post("/arm", headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["detail"] == "foreign_origin"
    # GET (non-mutating) is not origin-gated
    assert client.get("/state").status_code == 200


def test_tc_nfr_06_token_enforced_when_set():
    """TC-NFR-06: with a token set, a mutating request without the header is
    rejected; the documented header path succeeds."""
    client, state, _ = _client(api_token="secret123")
    state.entry_schedule = [{"time": "10:00"}]
    assert client.post("/arm", headers={"origin": PANEL}).status_code == 401  # no token
    ok = client.post("/arm", headers={"origin": PANEL, "x-api-token": "secret123"})
    assert ok.status_code == 200


def test_auth_check_confirms_the_password_without_side_effects():
    """NFR-06: /auth/check is a side-effect-free authenticated ping the UI uses to
    tell the operator whether the User Password is right — 200 when it matches,
    401 when it doesn't, and it never mutates state."""
    client, state, _ = _client(api_token="secret123")
    state.armed = False

    # wrong / missing token -> 401 (same gate as any mutating call)
    assert client.post("/auth/check", headers={"origin": PANEL}).status_code == 401
    bad = client.post("/auth/check", headers={"origin": PANEL, "x-api-token": "nope"})
    assert bad.status_code == 401

    # right token -> 200 {ok: true}, and nothing changed
    ok = client.post("/auth/check", headers={"origin": PANEL, "x-api-token": "secret123"})
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    assert state.armed is False


def test_auth_check_passes_when_no_password_is_required():
    """A localhost paper bind sets no token; /auth/check returns 200 for anyone,
    because there is nothing to prove."""
    client, _, _ = _client(api_token=None)
    assert client.post("/auth/check", headers={"origin": PANEL}).status_code == 200


def test_tc_nfr_06_non_localhost_bind_requires_token():
    """TC-NFR-06: config with bind_host != 127.0.0.1 and no api_token fails
    validation — structurally cannot expose the panel unauthenticated."""
    with pytest.raises(ConfigRejected) as ei:
        validate_bind("0.0.0.0", None)
    assert ei.value.reason == "non_localhost_requires_token"
    validate_bind("0.0.0.0", "a-token")  # with a token: ok
    validate_bind("127.0.0.1", None)     # localhost: token optional


# --- TC-UI-01: backend-authoritative config validation -----------------------

def test_tc_ui_01_config_validation_server_side():
    """TC-UI-01: backend rejects out-of-range config regardless of client;
    stop pct accepts exactly {95..300 step 5}; per_side is STP-02d-gated."""
    client, _, _ = _client()
    h = {"origin": PANEL}
    assert client.post("/config", json={"stop_loss_pct": 95}, headers=h).status_code == 200
    assert client.post("/config", json={"stop_loss_pct": 300}, headers=h).status_code == 200
    assert client.post("/config", json={"stop_loss_pct": 96}, headers=h).status_code == 422  # not in set
    assert client.post("/config", json={"stop_loss_pct": 301}, headers=h).status_code == 422
    r = client.post("/config", json={"stop_basis": "per_side"}, headers=h)  # STP-02d
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "allocation_unverified"
    assert client.post("/config", json={"stop_basis": "total_credit"}, headers=h).status_code == 200


# --- TC-UI-02: dashboard state contract --------------------------------------

def test_tc_ui_02_dashboard_state_contract():
    """TC-UI-02: dashboard state includes mode, kill state, protection/enable
    state; the blocking state is named when idle."""
    client, state, _ = _client()
    s = client.get("/state").json()
    assert set(s) >= {"armed", "stop_trading", "confirm_live", "trading_mode",
                      "entries_enabled", "blocking_state"}
    assert s["trading_mode"] == "paper"
    assert s["entries_enabled"] is False and s["blocking_state"] == "DISARMED"


def test_websocket_read_model_snapshot_and_origin_guard():
    """doc 05 §8: the WS pushes a read-model snapshot; NFR-06 refuses a foreign
    Origin on the upgrade."""
    client, state, _ = _client()
    with client.websocket_connect("/ws", headers={"origin": PANEL}) as ws:
        snap = ws.receive_json()
        assert "state" in snap and "report" in snap
        assert snap["state"]["trading_mode"] == "paper"
        ws.send_text("ping")
        assert "state" in ws.receive_json()
    # foreign Origin on the upgrade is refused
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_arm_disarm_and_report_endpoints():
    client, state, events = _client()
    h = {"origin": PANEL}
    # arming an empty schedule is rejected (ENT-01a)
    assert client.post("/arm", headers=h).status_code == 400
    state.entry_schedule = [{"time": "10:00"}]
    state.confirm_live = True
    armed = client.post("/arm", headers=h).json()
    assert armed["armed"] is True and armed["entries_enabled"] is True
    # stop trading blocks; report endpoint returns the day-report shape
    client.post("/stop-trading?on=true", headers=h)
    assert client.get("/state").json()["blocking_state"] == "STOP_TRADING"
    rpt = client.get("/report").json()
    assert set(rpt) >= {"entries_filled", "day_pnl", "stops_hit", "skips"}


# --- NFR-06 (2): "the panel's OWN host" includes its port ----------------------

def test_tc_nfr_06_the_panels_own_origin_including_its_port_is_allowed():
    """The bug: panel_origin defaulted to a PORTLESS "http://127.0.0.1", so the
    browser's own "http://127.0.0.1:8010" was refused as foreign. Every mutating
    request from the panel -- Save, Arm, Stop Trading, Close, Flatten, Fire --
    came back 403 foreign_origin. Security theatre that only fired on the
    legitimate user."""
    from meic.adapters.api.app import origin_allowed

    panel = "http://127.0.0.1"          # the old portless default
    for host in ("127.0.0.1:8010", "127.0.0.1:8000", "localhost:5173", "127.0.0.1"):
        assert origin_allowed(f"http://{host}", scheme="http", host=host, panel_origin=panel)


def test_tc_nfr_06_a_foreign_origin_is_still_refused():
    from meic.adapters.api.app import origin_allowed

    for evil in ("https://evil.example", "http://127.0.0.1.evil.example",
                 "http://evil.example:8010", "null"):
        assert not origin_allowed(evil, scheme="http", host="127.0.0.1:8010",
                                  panel_origin="http://127.0.0.1")


def test_tc_nfr_06_dns_rebinding_cannot_launder_a_foreign_origin():
    """An attacker who points their own domain at 127.0.0.1 sends Origin == Host.
    The Host is theirs, not loopback, so same-origin does NOT save them. This is
    exactly why the rule is `origin == own origin AND host is loopback`."""
    from meic.adapters.api.app import origin_allowed

    assert not origin_allowed("http://evil.example:8010", scheme="http",
                              host="evil.example:8010", panel_origin="http://127.0.0.1")


def test_tc_nfr_06_a_request_with_no_origin_is_not_a_browser():
    """The documented curl fallback (UI-09/17) sends no Origin. It is still
    token-gated whenever a token is set -- that check is separate."""
    from meic.adapters.api.app import origin_allowed

    assert origin_allowed(None, scheme="http", host="127.0.0.1:8010",
                          panel_origin="http://127.0.0.1")


def test_tc_nfr_06_an_explicitly_configured_panel_origin_still_wins():
    """A reverse proxy fronting the panel is named exactly, and needs no loopback."""
    from meic.adapters.api.app import origin_allowed

    assert origin_allowed("https://panel.internal", scheme="http",
                          host="10.0.0.5:8000", panel_origin="https://panel.internal")


def test_tc_nfr_06_save_from_the_browsers_own_origin_succeeds():
    """End to end through the middleware, at the port the panel actually runs on."""
    client, _state, _events = _client()
    origin = "http://127.0.0.1:8010"
    r = client.post("/schedule", json={"rows": [{"time": "10:00"}], "max_day_risk": "20000"},
                    headers={"origin": origin, "host": "127.0.0.1:8010"})
    assert r.status_code == 200 and r.json()["config_version"] == "v1"

    evil = client.post("/schedule", json={"rows": [{"time": "10:00"}]},
                       headers={"origin": "https://evil.example", "host": "127.0.0.1:8010"})
    assert evil.status_code == 403 and evil.json()["detail"] == "foreign_origin"


# --- FEATURE 1/2: card carries placed_at, legs, premium_received --------------

def test_get_entries_card_carries_placed_at_legs_and_premium_received():
    client, _state, events = _client()
    events.append(CondorFilled(
        entry_id="e1", net_credit=D("3.60"), at="2026-07-09T14:32:00+00:00",
        legs=(
            FilledLeg(symbol="SPXW260709P07535000", right="P", role="short", qty=1, price=D("1.80")),
            FilledLeg(symbol="SPXW260709P07510000", right="P", role="long", qty=1, price=D("0.08")),
            FilledLeg(symbol="SPXW260709C07540000", right="C", role="short", qty=1, price=D("1.95")),
            FilledLeg(symbol="SPXW260709C07565000", right="C", role="long", qty=1, price=D("0.07")),
        )))
    cards = client.get("/entries").json()
    assert len(cards) == 1
    c = cards[0]
    assert c["placed_at"] == "2026-07-09T14:32:00+00:00"
    assert len(c["legs"]) == 4
    put_short = next(l for l in c["legs"] if l["side"] == "PUT" and l["role"] == "short")
    assert put_short["strike"] == "7535" and put_short["price"] == "1.80" and put_short["qty"] == 1
    assert c["premium_received"] == {"PUT": "1.72", "CALL": "1.88"}


def test_get_entries_placed_at_and_legs_are_null_empty_when_absent():
    """Schema evolution / paper fills: no `at`, no allocated legs -> honest nulls,
    never a fabricated timestamp or premium."""
    client, _state, events = _client()
    events.append(CondorFilled(entry_id="e1", net_credit=D("4.00")))
    c = client.get("/entries").json()[0]
    assert c["placed_at"] is None
    assert c["legs"] == []
    assert c["premium_received"] == {"PUT": None, "CALL": None}


def test_get_entries_only_shows_todays_cards_2026_07_13_day_scope_fix():
    """2026-07-13 fix: /entries used to show every entry EVER logged, no day
    filter at all -- so a prior day's entry that never reached a terminal
    state (its settlement never captured) lingered on the board forever.
    `commands` isn't wired here, so the fallback source of "today" is the
    fold's own `state.date` (the most recent DayArmed) -- 2026-07-13 below."""
    client, _state, events = _client()
    events.append(DayArmed(date="2026-07-10", entry_count=1))
    events.append(CondorFilled(entry_id="2026-07-10#1", net_credit=D("5.20")))
    events.append(DayArmed(date="2026-07-13", entry_count=2))
    events.append(CondorFilled(entry_id="2026-07-13#2", net_credit=D("2.80")))

    cards = client.get("/entries").json()

    assert [c["entry_id"] for c in cards] == ["2026-07-13#2"]


# --- FEATURE 3: the entries_enricher hook, and that /ws reuses it -------------

def test_entries_enricher_hook_enriches_both_rest_and_websocket_snapshot():
    state = PersistentState(InMemoryStateStore())
    events: list = []

    def enricher(cards):
        for c in cards:
            c["live_pnl"] = "999"
        return cards

    app = create_app(state, events, panel_origin=PANEL, entries_enricher=enricher)
    client = TestClient(app)
    events.append(CondorFilled(entry_id="e1", net_credit=D("4.00")))

    assert client.get("/entries").json()[0]["live_pnl"] == "999"
    with client.websocket_connect("/ws", headers={"origin": PANEL}) as ws:
        snap = ws.receive_json()
        assert snap["entries"][0]["live_pnl"] == "999"


# --- activity feed day-separator feature (2026-07-15): additive `at`/`date` --
# fields so the frontend can group by ET trading day (DAY-03) honestly, never
# guessing from the browser's local date or fabricating a day for an event
# that carries none.

def test_activity_carries_at_when_the_event_has_one():
    """CondorFilled/ShortStopped/etc. carry an ORD-11 `at` -- the feed line
    mirrors it verbatim so the frontend can derive the ET day from the SAME
    instant, never a second computation that could drift."""
    client, _state, events = _client()
    events.append(CondorFilled(entry_id="2026-07-14#1", net_credit=D("3.60"),
                                at="2026-07-14T23:30:00+00:00"))
    line = client.get("/activity").json()[0]
    assert line["at"] == "2026-07-14T23:30:00+00:00"
    assert line["date"] is None


def test_activity_carries_date_for_events_with_no_at():
    """DayArmed has no `at` at all, only its own `date` -- the feed line
    mirrors THAT field so the frontend never has to fall back further than
    necessary."""
    client, _state, events = _client()
    events.append(DayArmed(date="2026-07-14", entry_count=2))
    line = client.get("/activity").json()[0]
    assert line["at"] is None
    assert line["date"] == "2026-07-14"


def test_activity_both_null_when_the_event_has_neither():
    """ModeSwitchStaged carries neither `at` nor `date` nor even an entry_id --
    honest nulls, never a fabricated timestamp (the frontend inherits the
    preceding item's day instead)."""
    from meic.domain.events import ModeSwitchStaged

    client, _state, events = _client()
    events.append(ModeSwitchStaged(target="live", effective="next_day"))
    line = client.get("/activity").json()[0]
    assert line["at"] is None
    assert line["date"] is None
    assert line["entry"] == ""


def test_activity_carries_the_event_type_key():
    """UI-31 (v1.73, queue slice 5): every /activity line carries the additive
    `type` field = the domain event's CLASS NAME, exactly as it keys the
    `_describe` table. The frontend's tooltip vocabulary
    (frontend/src/activityVocabulary.ts) looks explanations up by THIS key --
    never by the human-readable `label`, which is free to reword. Pinned
    against the REAL app response (journal real events through create_app,
    read the real payload) so the frontend-backend contract isn't proven only
    by regex-over-source and hand-built fixtures."""
    from meic.domain.events import ShortStopped

    client, _state, events = _client()
    events.append(DayArmed(date="2026-07-16", entry_count=1))
    events.append(CondorFilled(entry_id="2026-07-16#1", net_credit=D("4.00"),
                               at="2026-07-16T14:31:00+00:00"))
    events.append(ShortStopped(entry_id="2026-07-16#1", side="PUT",
                               fill=D("3.80"), slippage=D("0.00"),
                               at="2026-07-16T18:05:00+00:00"))
    lines = client.get("/activity").json()
    # newest-first: ShortStopped, CondorFilled, DayArmed
    assert [line["type"] for line in lines] == ["ShortStopped", "CondorFilled", "DayArmed"]
    # and the key is present as a real field on every line, alongside the label
    for line in lines:
        assert isinstance(line["type"], str) and line["type"]
