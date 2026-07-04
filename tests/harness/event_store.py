"""InMemoryEventStore — the harness EventStore (provisional port).

Crash/restart simulation (doc 04): the store, like the FakeBroker, lives
OUTSIDE the simulated bot instance. A test discards the bot, keeps this
object, boots a new instance against it — the persisted event log is intact,
which is what REC-01/02/03 and TC-RSK-07 scenarios exercise.
"""
from __future__ import annotations

from typing import Any


class InMemoryEventStore:
    def __init__(self) -> None:
        self._streams: dict[str, list[Any]] = {}

    def append(self, stream: str, events: list[Any]) -> None:
        self._streams.setdefault(stream, []).extend(events)

    def read(self, stream: str) -> list[Any]:
        return list(self._streams.get(stream, []))

    def streams(self) -> list[str]:
        return sorted(self._streams)
