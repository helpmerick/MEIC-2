"""TPF-03c — the evaluation COST INVARIANT.

"An evaluation pass MUST NOT perform work proportional to the day's event count
when the event log has not changed: the day projection is folded at most once
per unchanged log." This ships FIRST because it is the safety precondition for
any faster cadence -- at 250 ms an uncached `fold` is O(evaluations x events)
and degrades as the day grows, so it would PASS IN THE MORNING AND FAIL IN THE
AFTERNOON. That is the worst shape a performance defect can take, because the
test that would catch it is green when you run it.

The rule names the pin explicitly: "a test counting fold invocations across
repeated passes with no new events".
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from meic.application.event_log import DurableEventLog, EventLog
from meic.domain import projection
from meic.domain.events import CondorFilled, EntryClosed, FilledLeg
from meic.domain.projection import DayState, fold


def _legs():
    return (FilledLeg("SPXW  260720P07385000", "P", "long", 1),
            FilledLeg("SPXW  260720P07435000", "P", "short", 1),
            FilledLeg("SPXW  260720C07505000", "C", "short", 1),
            FilledLeg("SPXW  260720C07555000", "C", "long", 1))


def _log(n: int = 20) -> EventLog:
    log = EventLog()
    for i in range(n):
        log.append(CondorFilled(entry_id=f"2026-07-20#{i}", net_credit=D("3.60"), legs=_legs()))
    return log


class _CountingApply:
    """Counts per-EVENT work, which is what the invariant is actually about.

    Counting calls to `fold` itself would prove nothing -- `fold` is still
    called on every pass, and must be; what must not happen is the O(events)
    replay inside it."""

    def __init__(self, monkeypatch):
        self.events_applied = 0
        real = projection.apply

        def counting(state, event):
            self.events_applied += 1
            return real(state, event)

        monkeypatch.setattr(projection, "apply", counting)


@pytest.fixture
def counter(monkeypatch):
    return _CountingApply(monkeypatch)


def test_tpf03c_repeated_passes_over_an_unchanged_log_replay_the_events_once(counter):
    """THE PINNED TEST. 20 events, 100 passes: 20 applications, not 2000."""
    log = _log(20)
    for _ in range(100):
        fold(log)
    assert counter.events_applied == 20, (
        "an unchanged log was replayed more than once -- the exit loop's cost "
        "is proportional to the day's event count again")


def test_tpf03c_cost_does_not_grow_with_the_day(counter):
    """The failure mode stated as the rule states it: the SAME number of passes
    must not cost more in the afternoon than in the morning."""
    log = _log(10)
    for _ in range(50):
        fold(log)
    morning = counter.events_applied

    for i in range(200):                        # the day grows
        log.append(EntryClosed(entry_id=f"2026-07-20#{i}", initiator="eod"))
    counter.events_applied = 0

    for _ in range(50):
        fold(log)
    afternoon = counter.events_applied

    # One replay each way -- the afternoon's single replay is longer, but the
    # PER-PASS cost is what must not grow: 50 passes, one replay.
    assert morning == 10
    assert afternoon == 210, "the afternoon's 50 passes must still cost ONE replay"


def test_tpf03c_an_append_invalidates_the_cache(counter):
    """Correctness before speed: a cache that misses an append would evaluate
    exits against a stale projection -- strictly worse than no cache."""
    log = _log(5)
    first = fold(log)
    assert len(first.entries) == 5

    log.append(CondorFilled(entry_id="2026-07-20#new", net_credit=D("3.60"), legs=_legs()))
    second = fold(log)
    assert len(second.entries) == 6, "an appended event was not seen by the next fold"


def test_tpf03c_a_same_length_mutation_invalidates_the_cache():
    """`composition/runtime.py`'s drill reset does `events.clear()` then
    refills. A cache keyed on LENGTH alone would hand back the pre-reset day
    once the log reached its previous size again -- so the revision half of
    the key exists for exactly this."""
    log = _log(3)
    before = fold(log)
    assert set(before.entries) == {"2026-07-20#0", "2026-07-20#1", "2026-07-20#2"}

    log.clear()
    for i in range(3):                       # same LENGTH, entirely different day
        log.append(CondorFilled(entry_id=f"2026-07-21#{i}", net_credit=D("3.60"), legs=_legs()))

    after = fold(log)
    assert set(after.entries) == {"2026-07-21#0", "2026-07-21#1", "2026-07-21#2"}, (
        "a same-length clear-and-refill returned the previous day's projection")


def test_tpf03c_durable_log_appends_invalidate_too_despite_bypassing_the_overrides():
    """DurableEventLog.append calls `list.append(self, ...)` DIRECTLY, to keep
    its journal-first ordering. A revision bumped only in EventLog.append would
    therefore have gone stale in LIVE and nowhere else -- green in every paper
    test, wrong in production. This is that regression."""
    class _Journal:
        def __init__(self):
            self.written = []

        def append(self, item):
            self.written.append(item)

    log = DurableEventLog(journal=_Journal())
    log.append(CondorFilled(entry_id="2026-07-20#1", net_credit=D("3.60"), legs=_legs()))
    assert len(fold(log).entries) == 1

    log.append(CondorFilled(entry_id="2026-07-20#2", net_credit=D("3.60"), legs=_legs()))
    assert len(fold(log).entries) == 2, (
        "DurableEventLog's journal-first append bypassed cache invalidation")

    log.extend([CondorFilled(entry_id="2026-07-20#3", net_credit=D("3.60"), legs=_legs())])
    assert len(fold(log).entries) == 3, "DurableEventLog.extend bypassed cache invalidation"


def test_tpf03c_a_plain_list_is_uncached_never_incorrect(counter):
    """Every `fold(scoped)` over a filtered slice passes a plain list. It has
    no revision, so it is NOT cached -- the same cost as before, and never a
    wrong answer. Caching it would need a content hash, i.e. the O(events) work
    this rule exists to remove."""
    events = list(_log(5))
    fold(events)
    fold(events)
    assert counter.events_applied == 10, "a plain list must not be cached"


def test_tpf03c_the_cached_state_equals_a_fresh_replay():
    """The cache must be indistinguishable from replaying, not merely fast."""
    log = _log(8)
    log.append(EntryClosed(entry_id="2026-07-20#3", initiator="take_profit"))
    cached = fold(log)

    fresh = DayState()
    for event in list(log):
        fresh = projection.apply(fresh, event)

    assert cached.entries.keys() == fresh.entries.keys()
    assert cached.date == fresh.date
    for entry_id, entry in fresh.entries.items():
        assert cached.entries[entry_id].close_initiator == entry.close_initiator
