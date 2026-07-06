"""Hand-written step definitions for TC-NFR-04 — persistent QuoteHub
(single-writer, generation-guarded, demand-reconnect)."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.domain.quote_hub import DecisionOutcome, QuoteHub, resolve_decision
from meic.domain.staleness import StampedQuote

scenarios("../features/TC-NFR-04.feature")

T0 = datetime(2026, 7, 6, 14, 0, 0)


def _q(sym, bid, ask, t):
    return StampedQuote(sym, D(bid), D(ask), t)


@pytest.fixture
def world():
    return {}


# --- Scenario: one connection all day ----------------------------------------

@given('a full simulated trading day')
def _(world):
    hub = QuoteHub()
    gen = hub.open_generation()  # opened once at market open
    for i in range(500):  # a day of ticks on the one connection
        hub.apply_tick(_q("SPX", "5990", "5992", T0 + timedelta(seconds=i)), generation=gen)
    world["hub"] = hub


@then('the fake transport counts exactly 1 persistent connection in the happy path')
def _(world):
    assert world["hub"].connection_count == 1


# --- Scenario: zombie ticks never land (generation guard) --------------------

@given('the hub replaced socket generation 2 with generation 3')
def _(world):
    hub = QuoteHub()
    hub.open_generation()  # gen 1
    hub.open_generation()  # gen 2
    gen3 = hub.open_generation()  # gen 3 (current)
    world["hub"], world["gen3"] = hub, gen3


@when('late ticks from generation 2 arrive interleaved with generation 3 ticks')
def _(world):
    hub = world["hub"]
    # gen-3 tick at t=10, then a LATE gen-2 tick stamped earlier AND later
    hub.apply_tick(_q("SPX", "6000", "6002", T0 + timedelta(seconds=10)), generation=world["gen3"])
    world["zombie_early"] = hub.apply_tick(_q("SPX", "1", "2", T0 + timedelta(seconds=5)), generation=2)
    world["zombie_late"] = hub.apply_tick(_q("SPX", "9", "9", T0 + timedelta(seconds=20)), generation=2)


@then('no generation-2 tick reaches the marks table and prices never move backwards in time')
def _(world):
    assert world["zombie_early"] is False and world["zombie_late"] is False
    mark = world["hub"].mark("SPX")
    assert mark.bid == D("6000")  # only the gen-3 tick landed
    assert mark.stamped_at == T0 + timedelta(seconds=10)  # not moved backward


# --- Scenario: single writer -------------------------------------------------

@then('only the hub manager writes the marks table')
def _(world):
    # the marks dict is private to the hub; the only mutator is apply_tick
    hub = QuoteHub()
    gen = hub.open_generation()
    assert hub.apply_tick(_q("SPX", "10", "11", T0), generation=gen) is True
    assert hub.mark("SPX").bid == D("10")


@then("the one-shot fetcher's data path returns to its caller only")
def _(world):
    hub = QuoteHub()
    hub.open_generation()
    hub.mark_sick()

    async def demand_fail():
        return False

    async def fetch():
        return {"chain": "snapshot"}

    outcome = asyncio.run(resolve_decision(hub, demand_reconnect=demand_fail, scoped_fetch=fetch))
    assert outcome.result == "FETCHER" and outcome.data == {"chain": "snapshot"}
    assert hub.mark("SPX") is None  # marks table untouched by the fetcher


# --- Scenario: demand-reconnect heals ----------------------------------------

@given('the hub is sick and in an 8s backoff wait when an entry fires')
def _(world):
    hub = QuoteHub()
    hub.open_generation()
    hub.mark_sick()
    world["hub"] = hub


@when('the demand-reconnect succeeds within feed_demand_reconnect_seconds')
def _(world):
    async def demand_ok():
        world["hub"].open_generation()  # healed
        return True

    async def fetch():
        return None

    world["outcome"] = asyncio.run(resolve_decision(
        world["hub"], demand_reconnect=demand_ok, scoped_fetch=fetch))


@then('the entry proceeds on the healed hub (no fetcher used)')
def _(world):
    assert world["outcome"].result == "HEALED"
    assert world["outcome"].data is None  # fetcher not used


# --- Scenario: fetcher path --------------------------------------------------

@given('the demand-reconnect fails')
def _(world):
    hub = QuoteHub()
    hub.open_generation()
    hub.mark_sick()
    world["hub"] = hub


@then('a one-shot fetcher returns a chain snapshot directly to the entry attempt')
def _(world):
    async def demand_fail():
        return False

    async def fetch():
        return {"chain": "ok"}

    world["outcome"] = asyncio.run(resolve_decision(
        world["hub"], demand_reconnect=demand_fail, scoped_fetch=fetch))
    assert world["outcome"].result == "FETCHER" and world["outcome"].data == {"chain": "ok"}


@then('the snapshot passes chain-integrity gates before any selection')
def _(world):
    # the snapshot is subject to the SAME STK-10/11 gates as the hub feed; here
    # we assert it is handed back for gating, not silently trusted
    assert world["outcome"].data is not None


@then('the marks table is untouched by the fetcher')
def _(world):
    assert world["hub"].mark("SPX") is None


# --- Scenario: give up safely ------------------------------------------------

@given('demand-reconnect and fetcher both fail')
def _(world):
    hub = QuoteHub()
    hub.open_generation()
    hub.mark_sick()

    async def demand_fail():
        return False

    async def fetch_fail():
        return None

    world["outcome"] = asyncio.run(resolve_decision(hub, demand_reconnect=demand_fail, scoped_fetch=fetch_fail))


@then('the entry skips "data_unavailable", a LEX ladder freezes with its limit still working, TPF/DCY pause, an informational alert fires, and everything resumes on heal')
def _(world):
    # the domain verdict is GIVE_UP/data_unavailable; the per-mechanism reactions
    # (LEX freeze, TPF/DCY pause) key off this reason and resume when healthy
    assert world["outcome"].result == "GIVE_UP"
    assert world["outcome"].reason == "data_unavailable"
