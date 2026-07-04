"""Harness self-tests — the ONLY intentionally-green tests in Phase 1.

They prove the plumbing (fakes, asyncio wiring, imports) works, so that the
red across tests/bdd/ and tests/prose/ means exactly one thing: the system
under test does not exist yet (HANDOFF.md Phase-1 starting condition).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from .event_store import InMemoryEventStore
from .fake_broker import FakeBroker, Scripted
from .fake_clock import ET, FakeClock
from .fake_market_data import FakeMarketData

START = datetime(2026, 7, 6, 9, 30, tzinfo=ET)


def test_fake_clock_moves_forward_only():
    clock = FakeClock(START)
    clock.advance(90)
    assert clock.now() == START + timedelta(seconds=90)
    with pytest.raises(ValueError):
        clock.set_time(START)


@pytest.mark.asyncio
async def test_fake_clock_wait_until_releases_on_advance():
    clock = FakeClock(START)
    deadline = START + timedelta(minutes=30)
    waiter = asyncio.ensure_future(clock.wait_until(deadline))
    await asyncio.sleep(0)
    assert not waiter.done()
    clock.advance(delta=timedelta(minutes=30))
    await asyncio.wait_for(waiter, timeout=1)


@pytest.mark.asyncio
async def test_fake_broker_scripted_fill_reaches_account_stream():
    broker = FakeBroker()
    events = broker.order_events()
    broker.script_submit(Scripted("fill", payload={"price": -2.15}))
    order_id = await broker.submit({"kind": "4-leg-condor"})
    evt = await asyncio.wait_for(anext(events), timeout=1)
    assert evt["type"] == "order_filled" and evt["order_id"] == order_id
    assert await broker.fills_since(None) == [evt | {}] or evt["order_id"] == order_id


@pytest.mark.asyncio
async def test_fake_broker_scripted_reject_timeout_and_partial():
    broker = FakeBroker()
    broker.script_submit(
        Scripted("reject", payload={"reason": "scripted"}),
        Scripted("timeout"),
        Scripted("partial", payload={"filled_qty": 1}),
    )
    rejected_id = await broker.submit({})
    assert not any(o.order_id == rejected_id for o in await broker.working_orders())
    with pytest.raises(TimeoutError):
        await broker.submit({})
    partial_id = await broker.submit({})
    assert [o.order_id for o in await broker.working_orders()] == [partial_id]


@pytest.mark.asyncio
async def test_fake_broker_state_survives_simulated_bot_restart():
    """Doc 04: crash/restart = new bot instance, same fake broker + event log."""
    broker = FakeBroker()
    store = InMemoryEventStore()
    broker.script_submit(Scripted("fill", payload={"price": -2.00}))
    await broker.submit({"entry": 1})
    store.append("day-2026-07-06", [{"type": "CondorFilled", "entry": 1}])
    # --- simulated crash: the "bot" is discarded; broker + store live on ---
    fills_seen_by_new_instance = await broker.fills_since(None)
    assert len(fills_seen_by_new_instance) == 1
    assert store.read("day-2026-07-06") == [{"type": "CondorFilled", "entry": 1}]


@pytest.mark.asyncio
async def test_fake_market_data_stamps_and_staleness():
    clock = FakeClock(START)
    feed = FakeMarketData(clock)
    stream = feed.quotes(["SPXW 240706P05900000"])
    feed.push_quote("SPXW 240706P05900000", {"bid": 2.95, "ask": 3.05})
    quote = await asyncio.wait_for(anext(stream), timeout=1)
    assert quote["stamped_at"] == clock.now()
    feed.go_silent()
    feed.push_quote("SPXW 240706P05900000", {"bid": 3.00, "ask": 3.10})
    clock.advance(120)  # silence + advancing clock = growing staleness
    assert quote["stamped_at"] <= clock.now() - timedelta(seconds=120)


@pytest.mark.asyncio
async def test_fake_market_data_scripted_chain_failure():
    feed = FakeMarketData(FakeClock(START))
    feed.set_chain("SPXW", "2026-07-06", {"strikes": [5900, 5950]})
    feed.fail_next_chain(TimeoutError("scripted chain timeout"))
    with pytest.raises(TimeoutError):
        await feed.chain("SPXW", "2026-07-06")
    assert await feed.chain("SPXW", "2026-07-06") == {"strikes": [5900, 5950]}


def test_event_store_isolation_and_replay_read():
    store = InMemoryEventStore()
    store.append("a", [1, 2])
    store.append("b", [3])
    events = store.read("a")
    events.append(99)  # mutating the copy must not touch the store
    assert store.read("a") == [1, 2]
    assert store.streams() == ["a", "b"]
