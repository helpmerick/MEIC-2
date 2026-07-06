"""SqliteEventStore + SqliteStateStore — durability and crash/restart (REC-01)."""
from decimal import Decimal as D

from meic.adapters.persistence.event_store import SqliteEventStore, SqliteStateStore
from meic.domain.events import CondorFilled, DayArmed, ShortStopped


def test_append_read_roundtrip_preserves_decimals(tmp_path):
    store = SqliteEventStore(tmp_path / "log.db")
    store.append("day-2026-07-06", [
        DayArmed(date="2026-07-06", entry_count=3),
        CondorFilled(entry_id="e1", net_credit=D("2.30")),
        ShortStopped(entry_id="e1", side="PUT", fill=D("3.80"), slippage=D("0.15")),
    ])
    events = store.read("day-2026-07-06")
    assert [e.type for e in events] == ["DayArmed", "CondorFilled", "ShortStopped"]
    # Decimals survive exactly — never floated
    assert events[1].net_credit == D("2.30") and isinstance(events[1].net_credit, D)
    assert events[2].fill == D("3.80") and events[2].slippage == D("0.15")


def test_append_only_ordering_within_stream(tmp_path):
    store = SqliteEventStore(tmp_path / "log.db")
    for i in range(5):
        store.append("s", [DayArmed(date=f"d{i}", entry_count=i)])
    assert [e.entry_count for e in store.read("s")] == [0, 1, 2, 3, 4]


def test_streams_are_isolated(tmp_path):
    store = SqliteEventStore(tmp_path / "log.db")
    store.append("mon", [DayArmed(date="2026-07-06", entry_count=1)])
    store.append("tue", [DayArmed(date="2026-07-07", entry_count=2)])
    assert store.streams() == ["mon", "tue"]
    assert len(store.read("mon")) == 1 and store.read("mon")[0].entry_count == 1


def test_log_survives_process_death(tmp_path):
    """Crash/restart = close this object, open a NEW one on the same file."""
    path = tmp_path / "log.db"
    s1 = SqliteEventStore(path)
    s1.append("day", [DayArmed(date="2026-07-06", entry_count=6),
                       CondorFilled(entry_id="e1", net_credit=D("4.00"))])
    s1.close()  # the process dies

    s2 = SqliteEventStore(path)  # a fresh instance boots on the same log
    events = s2.read("day")
    assert len(events) == 2 and events[1].net_credit == D("4.00")


def test_state_store_durable_kv(tmp_path):
    path = tmp_path / "state.db"
    s1 = SqliteStateStore(path)
    s1.set("armed", "true")
    s1.set("armed", "false")  # last write wins
    s1.close()
    s2 = SqliteStateStore(path)
    assert s2.get("armed") == "false"
    assert s2.get("never_set") is None
