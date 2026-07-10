"""The schedule panel's HTTP surface: GET/POST /schedule, /preflight, /arm,
and the ENT-09 ▶ endpoints. No trading logic in the frontend (UI-03) — every
rule below is enforced server-side.
"""
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from meic.adapters.api.app import create_app
from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState


class _Commands:
    """Only what the schedule/fire routes touch."""

    def __init__(self, *, can_fire=True, preflight_checks=None, day="2026-07-10"):
        self._can_fire = can_fire
        self.preflight_checks = preflight_checks
        self.fired: list = []
        self.fired_floors: list = []   # ENT-09b v1.57: (put_floor, call_floor) per fire() call
        self.simulated: list = []
        self._day = day

    def can_fire(self):
        return self._can_fire

    def fire_preview(self, n, row):
        from meic.application.manual_entry import FirePreview
        from meic.application.schedule_service import worst_case_estimate
        return FirePreview(press_id=f"press-{n}", entry_number=n, now="2026-07-06T10:07:00",
                           contracts=row.contracts, target_premium=row.target_premium,
                           wing_width=row.wing_width, stop_loss_pct=row.stop_loss_pct,
                           worst_case_estimate=worst_case_estimate(row))

    async def fire(self, *, press_id, entry_number, row, confirmed, put_floor=None, call_floor=None):
        self.fired.append((press_id, entry_number, confirmed))
        self.fired_floors.append((put_floor, call_floor))   # ENT-09b v1.57
        if not confirmed:
            return {"result": "not_confirmed"}
        return {"result": "filled", "entry_id": f"d#{entry_number}", "initiator": "manual_entry"}

    # --- ENT-09b v1.57 -----------------------------------------------------------
    def floor_candidates(self, row):
        return {"available": False}

    # --- ENT-11/UI-25 ad-hoc manual trade ---------------------------------------
    async def simulate(self, row):
        self.simulated.append(row)
        return {"result": "ok", "put_short": "5990", "put_long": "5940",
                "call_short": "6060", "call_long": "6110", "put_mid": "3.10",
                "call_mid": "2.90", "net_credit": "4.00", "worst_case": "4600",
                "contracts": row.contracts,
                "estimate_note": "simulation — the real fire re-selects from fresh data and may differ"}

    def day(self):
        return self._day


def _client(rows=None, *, commands=None, mode="paper", max_day_risk=None, events=None):
    state = PersistentState(InMemoryStateStore())
    state.trading_mode = mode
    if rows is not None:
        state.entry_schedule = rows
    if max_day_risk is not None:
        state.max_day_risk = str(max_day_risk)
    app = create_app(state, events if events is not None else [], commands=commands or _Commands())
    return TestClient(app), state


def _row(t="10:00", **over):
    return {"time": t, **over}


# --- GET /schedule ------------------------------------------------------------------

def test_get_schedule_shows_estimates_the_ceiling_and_the_headroom():
    c, _ = _client([_row("10:00"), _row("11:00")], max_day_risk=D("20000"))
    body = c.get("/schedule").json()
    assert D(body["day_total_estimate"]) == D("9400")     # 2 x (50 - 3) x 100
    assert D(body["max_day_risk"]) == D("20000")
    assert D(body["headroom"]) == D("10600")
    assert body["exceeds_max_day_risk"] is False
    assert [D(r["worst_case_estimate"]) for r in body["rows"]] == [D("4700"), D("4700")]


def test_get_schedule_labels_the_number_an_estimate():
    c, _ = _client([_row()])
    assert "ESTIMATED" in c.get("/schedule").json()["estimate_note"]


def test_the_panel_warns_when_the_composed_day_exceeds_the_ceiling():
    c, _ = _client([_row("10:00", contracts=5)], max_day_risk=D("20000"))
    body = c.get("/schedule").json()
    assert body["exceeds_max_day_risk"] is True and D(body["headroom"]) < 0


# --- POST /schedule -----------------------------------------------------------------

def test_saving_a_valid_schedule_versions_and_persists_it():
    c, state = _client([])
    r = c.post("/schedule", json={"rows": [_row("10:00", contracts=2)], "max_day_risk": "15000"})
    assert r.status_code == 200
    assert r.json()["config_version"] == "v1"
    assert state.entry_schedule[0]["contracts"] == 2
    assert state.max_day_risk == "15000"


def test_an_invalid_schedule_is_422_with_every_error_and_persists_nothing():
    c, state = _client([])
    r = c.post("/schedule", json={"rows": [_row("10:00", contracts=11),
                                           _row("09:00", stop_loss_pct=97)]})
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert {e["field"] for e in errors} >= {"contracts", "stop_loss_pct"}
    assert state.entry_schedule == [] and state.config_version is None


def test_per_side_is_refused_at_the_row_level():
    c, _ = _client([])
    r = c.post("/schedule", json={"rows": [_row(stop_basis="per_side")]})
    assert r.status_code == 422
    assert any(e["reason"] == "allocation_unverified" for e in r.json()["detail"]["errors"])


# --- /preflight and /arm ------------------------------------------------------------

def test_preflight_passes_and_arm_arms():
    c, state = _client([_row()])
    pre = c.get("/preflight").json()
    assert pre["passed"] is True and pre["blocked_by"] is None

    r = c.post("/arm")
    assert r.status_code == 200 and state.armed is True
    assert r.json()["preflight"]["passed"] is True


def test_arming_an_empty_schedule_is_refused_with_the_checklist():
    c, state = _client([])
    r = c.post("/arm")
    assert r.status_code == 400
    assert r.json()["detail"]["blocked_by"] == "schedule"
    assert state.armed is False


def test_arming_is_refused_when_a_preflight_item_fails():
    cmds = _Commands(preflight_checks={"reconcile": lambda: (False, "mismatch open")})
    c, state = _client([_row()], commands=cmds)
    r = c.post("/arm")
    assert r.status_code == 400 and r.json()["detail"]["blocked_by"] == "reconcile"
    assert state.armed is False


def test_live_mode_arm_requires_max_day_risk():
    """doc 06 s169 — mandatory before live can be enabled."""
    c, state = _client([_row()], mode="live")
    r = c.post("/arm")
    assert r.status_code == 400 and r.json()["detail"]["blocked_by"] == "max_day_risk"
    assert state.armed is False

    ok, state2 = _client([_row()], mode="live", max_day_risk=D("20000"))
    assert ok.post("/arm").status_code == 200 and state2.armed is True


# --- ENT-09 ▶ endpoints ---------------------------------------------------------------

def test_fire_preview_returns_a_labelled_estimate_and_the_button_state():
    c, _ = _client([_row("10:00", contracts=2)])
    body = c.get("/entry/1/fire-preview").json()
    assert body["worst_case_is_estimate"] is True
    assert D(body["worst_case_estimate"]) == D("9400")     # (50 - 3) x 100 x 2
    assert body["can_fire"] is True
    assert body["press_id"]


def test_fire_preview_reports_the_button_disabled_when_gates_block():
    c, _ = _client([_row()], commands=_Commands(can_fire=False))
    assert c.get("/entry/1/fire-preview").json()["can_fire"] is False


def test_firing_requires_a_press_id():
    c, _ = _client([_row()])
    assert c.post("/entry/1/fire", json={"confirmed": True}).status_code == 400


def test_an_unconfirmed_fire_submits_nothing():
    cmds = _Commands()
    c, _ = _client([_row()], commands=cmds)
    r = c.post("/entry/1/fire", json={"press_id": "p1", "confirmed": False})
    assert r.json() == {"result": "not_confirmed"}
    assert cmds.fired == [("p1", 1, False)]


def test_a_confirmed_fire_runs_the_row_and_is_tagged_manual_entry():
    cmds = _Commands()
    c, _ = _client([_row("10:00"), _row("11:00")], commands=cmds)
    r = c.post("/entry/2/fire", json={"press_id": "p1", "confirmed": True})
    assert r.json()["initiator"] == "manual_entry"
    assert cmds.fired == [("p1", 2, True)]      # row 2, not row 1


def test_firing_an_unknown_row_is_404():
    c, _ = _client([_row()])
    assert c.post("/entry/9/fire", json={"press_id": "p1", "confirmed": True}).status_code == 404
    assert c.get("/entry/0/fire-preview").status_code == 404


# --- ENT-11/UI-25 ad-hoc manual trade -------------------------------------------

def test_manual_simulate_returns_the_simulation_through_the_api():
    cmds = _Commands()
    c, _ = _client([], commands=cmds)
    r = c.post("/manual/simulate", json={"contracts": 2, "target_premium": "3.00"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "ok"
    assert body["put_short"] == "5990" and body["net_credit"] == "4.00"
    assert cmds.simulated[0].contracts == 2   # the row the endpoint built and passed through


def test_manual_simulate_rejects_bad_params_with_422_shaped_errors():
    c, _ = _client([], commands=_Commands())
    r = c.post("/manual/simulate", json={"contracts": 11})
    assert r.status_code == 422
    assert any(e["field"] == "contracts" for e in r.json()["detail"]["errors"])


def test_manual_fire_allocates_a_101_plus_number_and_fires_through_commands():
    cmds = _Commands(day="2026-07-10")
    c, _ = _client([_row("10:00")], commands=cmds)  # a schedule row exists at 1 — must not collide
    r = c.post("/manual/fire", json={"press_id": "p1", "confirmed": True, "contracts": 2})
    assert r.status_code == 200
    assert r.json()["result"] == "filled"
    assert cmds.fired == [("p1", 101, True)]


def test_manual_fire_allocates_the_next_number_after_an_existing_ad_hoc_fill():
    from meic.domain.events import CondorFilled
    cmds = _Commands(day="2026-07-10")
    events = [CondorFilled(entry_id="2026-07-10#101", net_credit=D("4.00"))]
    c, _ = _client([], commands=cmds, events=events)
    c.post("/manual/fire", json={"press_id": "p1", "confirmed": True})
    assert cmds.fired[-1][1] == 102


def test_manual_fire_requires_confirmed():
    cmds = _Commands()
    c, _ = _client([], commands=cmds)
    r = c.post("/manual/fire", json={"press_id": "p1", "confirmed": False})
    assert r.json() == {"result": "not_confirmed"}
    assert cmds.fired == [("p1", 101, False)]  # unconfirmed still records nothing at the domain layer


def test_manual_fire_requires_a_press_id():
    c, _ = _client([], commands=_Commands())
    r = c.post("/manual/fire", json={"confirmed": True})
    assert r.status_code == 400
