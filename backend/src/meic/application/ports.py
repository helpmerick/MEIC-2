"""Ports (domain-defined interfaces) — doc 05 §6.

BrokerGateway and MarketDataFeed are transcribed verbatim from the spec.
The domain types they reference do not exist yet (Phase 1 builds no domain
logic): each is a placeholder alias below, to be replaced by real domain
value objects in the domain build phase. The fakes in tests/harness/ treat
them as opaque payloads.

Clock, EventStore, AlertSink and ExchangeCalendar are left unspecified in
doc 05 §6 (literal `...`). The members below are the PROVISIONAL minimum the
Phase-1 harness needs and are flagged in the Phase-1 PR summary; they get
firmed up (or amended) in the domain phase. No logic lives in this module.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Protocol

# ---------------------------------------------------------------------------
# Placeholder domain types (Phase 1 only — replaced by meic.domain VOs later).
# ---------------------------------------------------------------------------
OrderIntent = Any
BrokerOrderId = Any
CancelResult = Any
BrokerOrder = Any
BrokerPosition = Any
Fill = Any
OrderEvent = Any
ChainSnapshot = Any
Quote = Any
IndexTick = Any


class BrokerGateway(Protocol):  # implemented by TastytradeAdapter, FakeBroker
    async def submit(self, order: OrderIntent) -> BrokerOrderId: ...
    async def cancel(self, id) -> CancelResult: ...
    async def replace(self, id, new: OrderIntent) -> BrokerOrderId: ...
    async def working_orders(self) -> list[BrokerOrder]: ...
    async def positions(self) -> list[BrokerPosition]: ...
    async def fills_since(self, cursor) -> list[Fill]: ...
    def order_events(self) -> AsyncIterator[OrderEvent]: ...  # account stream
    # ORD-04/EC-API-03 (2026-07-17 security review finding A): after a submit()
    # exception, is `order` already resting/filled at the broker? The order's
    # `idempotency_key` is stamped onto the broker's server-side
    # `external_identifier` (TastytradeAdapter._build_order), so the match is
    # on OUR OWN unique client id -- never a leg-shape guess that
    # could adopt the operator's structurally-identical order on a shared
    # account (OWN-01/OWN-03). Returns the matching LIVE/filled order's id, or
    # None if no such order exists (the submit genuinely never landed).
    async def find_matching_order(self, order: OrderIntent) -> BrokerOrderId | None: ...


class MarketDataFeed(Protocol):  # DXLinkAdapter, FakeMarketData
    async def chain(self, underlying, expiration) -> ChainSnapshot: ...
    def quotes(self, symbols) -> AsyncIterator[Quote]: ...  # staleness-stamped
    def spot(self, index) -> AsyncIterator[IndexTick]: ...


class Clock(Protocol):  # SystemClock (NTP-checked, DAY-03), FakeClock
    # Provisional Phase-1 surface — doc 05 §6 leaves this port unspecified.
    def now(self) -> Any: ...
    async def wait_until(self, when: Any) -> None: ...


class EventStore(Protocol):  # SqliteEventStore, InMemoryEventStore
    # Provisional Phase-1 surface — doc 05 §6 leaves this port unspecified.
    def append(self, stream: str, events: list[Any]) -> None: ...
    def read(self, stream: str) -> list[Any]: ...
    def streams(self) -> list[str]: ...


class StateStore(Protocol):  # SqliteStateStore, InMemoryStateStore
    """Durable key/value backing the REC-07 persistent-state inventory.
    Values are opaque strings; typed access lives in
    application.persistent_state."""

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def all(self) -> dict[str, str]: ...


class AlertSink(Protocol):  # UI/webhook/email fan-out (RSK-06)
    # Provisional Phase-1 surface — doc 05 §6 leaves this port unspecified.
    def alert(self, level: str, message: str, **context: Any) -> None: ...


class ExchangeCalendar(Protocol):  # DAY-01/02
    # Provisional Phase-1 surface — doc 05 §6 leaves this port unspecified.
    def is_trading_day(self, date: Any) -> bool: ...
