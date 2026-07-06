"""Persistent-state inventory — REC-07 (and TC-ENT-07 persistence scenarios)."""
import pytest

from meic.adapters.persistence.event_store import InMemoryStateStore, SqliteStateStore
from meic.application.persistent_state import PersistentState


def test_fresh_install_defaults_are_safe():
    """TC-ENT-07 'Fresh install defaults safe': DISARMED, Stop Trading off,
    Confirm Live OFF; trading_mode defaults to paper."""
    s = PersistentState(InMemoryStateStore())
    assert s.armed is False
    assert s.stop_trading is False
    assert s.confirm_live is False
    assert s.trading_mode == "paper"
    assert s.entry_schedule == []
    assert s.tpf_floors == {}
    assert s.entries_enabled() is False
    assert s.blocking_state() == "DISARMED"


def test_entry_gate_requires_all_three_states():
    """Entries fire iff ARMED ∧ Stop Trading OFF ∧ Confirm Live ON (ENT-01b)."""
    s = PersistentState(InMemoryStateStore())
    s.armed = True
    assert s.entries_enabled() is False and s.blocking_state() == "CONFIRM_LIVE_OFF"
    s.confirm_live = True
    assert s.entries_enabled() is True and s.blocking_state() is None
    s.stop_trading = True
    assert s.entries_enabled() is False and s.blocking_state() == "STOP_TRADING"


def test_full_inventory_survives_docker_recovery(tmp_path):
    """TC-ENT-07: ARMED on, Stop Trading on, Confirm Live on, mode paper, a
    6-entry schedule, an armed TPF floor, a paper cash ledger — every item
    restored exactly after the container dies and recovers."""
    path = tmp_path / "state.db"
    store = SqliteStateStore(path)
    s = PersistentState(store)
    s.armed = True
    s.stop_trading = True
    s.confirm_live = True
    s.trading_mode = "paper"
    s.entry_schedule = [{"time": f"1{i}:00", "target_premium": "3.00"} for i in range(6)]
    s.tpf_floors = {"e1": 30}
    s.paper_cash_ledger = {"cash": "100000.00"}
    s.config_version = "1.41"
    store.close()  # container dies

    recovered = PersistentState(SqliteStateStore(path))  # container recovers
    assert recovered.armed is True
    assert recovered.stop_trading is True
    assert recovered.confirm_live is True
    assert recovered.trading_mode == "paper"
    assert len(recovered.entry_schedule) == 6
    assert recovered.tpf_floors == {"e1": 30}
    assert recovered.paper_cash_ledger == {"cash": "100000.00"}
    assert recovered.config_version == "1.41"
    # entries remain blocked (Stop Trading on) until the operator resumes
    assert recovered.entries_enabled() is False
    assert recovered.blocking_state() == "STOP_TRADING"


def test_disarmed_state_equally_persists(tmp_path):
    path = tmp_path / "state.db"
    s = PersistentState(SqliteStateStore(path))
    s.armed = True
    s.armed = False  # operator disarms
    recovered = PersistentState(SqliteStateStore(path))
    assert recovered.armed is False  # persists across restart, no daily reset


def test_extending_inventory_is_rejected():
    """REC-07: a state not on the list must not silently gain persistence."""
    s = PersistentState(InMemoryStateStore())
    with pytest.raises(KeyError):
        s._set("some_new_flag", True)


def test_trading_mode_validated():
    s = PersistentState(InMemoryStateStore())
    with pytest.raises(ValueError):
        s.trading_mode = "production"
