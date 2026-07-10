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

    def _stamp(self, item):
        if isinstance(item, Event) and self.config_version and not item.config_version:
            return item.stamped(self.config_version)
        return item

    def append(self, item) -> None:
        super().append(self._stamp(item))

    def extend(self, items) -> None:
        super().extend(self._stamp(i) for i in items)

    def insert(self, index: int, item) -> None:
        super().insert(index, self._stamp(item))


class DurableEventLog(EventLog):
    """An `EventLog` that write-throughs every append/extend to a durable
    journal AFTER the config-version stamp — REC-01 ("persisted... BEFORE
    being acted on") + REC-07 item 8.

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
        list.append(self, stamped)
        self._journal.append(stamped)

    def extend(self, items) -> None:
        stamped = [self._stamp(i) for i in items]
        list.extend(self, stamped)
        for i in stamped:
            self._journal.append(i)

    def insert(self, index: int, item) -> None:
        # Not used by any current caller (services only append/extend); refusing
        # rather than silently accepting an un-journaled insert keeps the
        # write-through guarantee absolute instead of quietly leaking an
        # untracked path the day someone reaches for it.
        raise NotImplementedError(
            "DurableEventLog.insert: no caller needs positional insert; append/extend only")
