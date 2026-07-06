"""FastAPI control panel — NFR-06 security + UI contract (TC-NFR-06/UI-01/02).

Uses the in-process TestClient — no network. Prose-TC functions named
test_tc_* so the traceability checker counts them.
"""
import pytest
from fastapi.testclient import TestClient

from meic.adapters.api.app import create_app
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.config.validation import ConfigRejected, validate_bind

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
