"""TC-REC-01 — REC-01/UI-10: replaying the event log reproduces identical day
state and P&L (property test across scripted days).

Hand-written prose-TC implementation (the generator skips prose TCs that have
a hand-written test module). Named test_tc_rec_01 so the traceability checker
counts it implemented.
"""
import random
from decimal import Decimal as D

from meic.adapters.persistence.event_store import SqliteEventStore
from meic.domain.events import (
    CondorFilled,
    DayArmed,
    LongSold,
    ShortStopped,
    SideExpired,
)
from meic.domain.projection import fold


def _scripted_day(seed: int):
    """Deterministically generate a plausible day's event log from a seed."""
    rng = random.Random(seed)
    events = [DayArmed(date="2026-07-06", entry_count=3)]
    for n in range(1, 4):
        eid = f"e{n}"
        credit = D(str(rng.choice(["2.30", "3.10", "4.00"])))
        events.append(CondorFilled(entry_id=eid, net_credit=credit))
        for side in ("PUT", "CALL"):
            if rng.random() < 0.5:  # this side stops
                fill = D(str(rng.choice(["3.80", "2.28", "5.07"])))
                events.append(ShortStopped(entry_id=eid, side=side, fill=fill, slippage=D("0.15")))
                events.append(LongSold(entry_id=eid, side=side, recovery=D(str(rng.choice(["0.00", "0.40"])))))
            else:
                events.append(SideExpired(entry_id=eid, side=side))
    return events


def test_tc_rec_01_replay_reproduces_identical_state_and_pnl():
    """Property: for many scripted days, folding the log twice — and folding a
    persisted-then-reloaded copy — yields an equal DayState and equal P&L."""
    for seed in range(50):
        events = _scripted_day(seed)
        first = fold(events)
        assert fold(events) == first  # replay determinism
        assert fold(events).day_pnl == first.day_pnl


def test_tc_rec_01_persisted_log_replays_identically(tmp_path):
    for seed in range(20):
        events = _scripted_day(seed)
        expected = fold(events)
        db = tmp_path / f"day-{seed}.db"
        store = SqliteEventStore(db)
        store.append("day", events)
        store.close()
        reloaded = fold(SqliteEventStore(db).read("day"))
        assert reloaded == expected
        assert reloaded.day_pnl == expected.day_pnl
