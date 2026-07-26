"""The event log the services append to.

v1.44 (operator-ratified: "build it NOW, not debt"): every domain event carries
the `config_version` in force when it was recorded, so a replayed log tells you
WHICH RULES produced each event. Config changes mid-day (doc 06 "next-entry"
scope), so an event log without version stamps cannot be audited after the fact:
you would not know whether an entry used a 95% or a 150% stop.

Stamping happens HERE, on append, rather than at ~40 `events.append(...)` call
sites. A stamp that each service must remember to apply is a stamp that will be
missing from exactly the event you need.

Services keep taking a plain `list` — this IS one — so nothing else changes.
"""
from __future__ import annotations

from typing import Iterable

from meic.domain.events import Event


class EventLog(list):
    """A list that stamps every appended Event with the current config_version.

    `config_version` is mutable: the operator may save new config mid-day, and
    events recorded after that point must carry the new version.
    """

    def __init__(self, iterable: Iterable = (), *, config_version: str = "") -> None:
        super().__init__(iterable)
        self.config_version = config_version
        # TPF-03c: bumped by every mutating path so `fold` can tell an
        # UNCHANGED log from a changed one. Paired with len() in the cache key
        # (see domain/projection.fold) because `list.append(self, x)` can and
        # DOES bypass these overrides -- DurableEventLog.append calls it
        # directly to keep its journal-first ordering. Revision catches a
        # same-length change (clear-then-refill); length catches a bypass.
        # Neither alone is sufficient, which is why both are in the key.
        self._revision = 0

    def _touch(self) -> None:
        self._revision += 1

    def _stamp(self, item):
        if isinstance(item, Event) and self.config_version and not item.config_version:
            return item.stamped(self.config_version)
        return item

    def append(self, item) -> None:
        super().append(self._stamp(item))
        self._touch()

    def extend(self, items) -> None:
        super().extend(self._stamp(i) for i in items)
        self._touch()

    def insert(self, index: int, item) -> None:
        super().insert(index, self._stamp(item))
        self._touch()

    # TPF-03c: EVERY remaining list mutator, enumerated rather than assumed.
    # An un-overridden mutator is a silent stale-projection path, so the list
    # is exhaustive by construction: if a new one appears in a future Python,
    # the len() half of the cache key still catches any change of length.
    def remove(self, item) -> None:
        super().remove(item)
        self._touch()

    def pop(self, index: int = -1):
        item = super().pop(index)
        self._touch()
        return item

    def clear(self) -> None:
        super().clear()
        self._touch()

    def sort(self, *a, **k) -> None:
        super().sort(*a, **k)
        self._touch()

    def reverse(self) -> None:
        super().reverse()
        self._touch()

    def __setitem__(self, index, value) -> None:
        super().__setitem__(index, value)
        self._touch()

    def __delitem__(self, index) -> None:
        super().__delitem__(index)
        self._touch()

    def __iadd__(self, other):
        result = super().__iadd__(other)
        self._touch()
        return result

    def __imul__(self, other):
        result = super().__imul__(other)
        self._touch()
        return result


class DurableEventLog(EventLog):
    """An `EventLog` that write-throughs every append/extend to a durable
    journal — REC-01 + REC-07 item 8.

    **Ordering pinned (v1.74, REC-01): JOURNAL-FIRST.** The event is durably
    written to the journal BEFORE it is appended to the in-memory list (i.e.
    before any in-memory state or actor can observe it). If the journal write
    raises, this method RAISES too and the in-memory list is left UNCHANGED —
    nothing ever acts on an event that durably never happened. The rejected
    alternative (in-memory-first, journal after) was found live: a journal
    failure there still left the in-memory event acted upon, so a restart
    replayed a shorter, DIFFERENT history than the one the process had just
    finished acting on — the process believed a lie until it next crashed.
    Journal-first means in-memory and durable state can never diverge: either
    both advance, or neither does.

    `journal` is duck-typed (only `.append(event) -> None` is required) so
    this application-layer class never imports the concrete adapter
    (`EventJournal`, adapters/persistence/event_store.py) — the composition
    root wires the real one in.
    """

    def __init__(self, iterable: Iterable = (), *, config_version: str = "", journal) -> None:
        super().__init__(iterable, config_version=config_version)
        self._journal = journal

    def append(self, item) -> None:
        stamped = self._stamp(item)
        self._journal.append(stamped)  # REC-01: journal FIRST; a raise here
        list.append(self, stamped)     # leaves the in-memory list untouched.
        self._touch()                  # TPF-03c: this path BYPASSES EventLog.append

    def extend(self, items) -> None:
        # REC-01: journal-then-memory PER ITEM, not as two separate batch
        # passes — a batch-journal-then-batch-memory split would let a
        # mid-batch journal failure leave later items durably written but
        # not yet in memory (or vice versa) for the span of this call. Per-
        # item interleaving keeps the two never more than one item apart,
        # and a raised item lands in NEITHER, matching `append`'s guarantee.
        for i in items:
            stamped = self._stamp(i)
            self._journal.append(stamped)
            list.append(self, stamped)
            self._touch()              # TPF-03c: bypasses EventLog.extend

    def insert(self, index: int, item) -> None:
        # Not used by any current caller (services only append/extend); refusing
        # rather than silently accepting an un-journaled insert keeps the
        # write-through guarantee absolute instead of quietly leaking an
        # untracked path the day someone reaches for it.
        raise NotImplementedError(
            "DurableEventLog.insert: no caller needs positional insert; append/extend only")
