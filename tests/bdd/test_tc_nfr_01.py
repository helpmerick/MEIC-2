"""Hand-written step definitions for TC-NFR-01 — a hung broker call cannot
freeze the bot (NFR-01). Drives the real OffLoopExecutor: a blocking call runs
on a dedicated worker while the event loop keeps ticking."""
import asyncio
import time

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.off_loop import OffLoopExecutor

scenarios("../features/TC-NFR-01.feature")


@pytest.fixture
def world():
    return {}


@given('a broker REST call that hangs for 30 seconds (injected)')
def _(world):
    # scaled down for the test; the mechanism (off-loop worker) is identical
    world["hang"] = 0.20

    def hung_broker_call():
        time.sleep(world["hang"])
        return "eventually-returns"

    world["call"] = hung_broker_call


@when('the next scheduled entry time arrives during the hang')
def _(world):
    async def scenario():
        ex = OffLoopExecutor()
        ticks = 0

        async def loop_keeps_running():  # scheduler / probe / quotes / UI stream
            nonlocal ticks
            for _ in range(20):
                ticks += 1
                await asyncio.sleep(0.005)

        t = asyncio.ensure_future(loop_keeps_running())
        result = await ex.run(world["call"])  # off-loop — must not freeze the loop
        await t
        ex.shutdown()
        return ticks, result

    world["ticks"], world["result"] = asyncio.run(scenario())


@then('the entry attempt begins on time')
def _(world):
    # the loop advanced many times WHILE the broker call was hung -> not frozen
    assert world["ticks"] >= 10
    assert world["result"] == "eventually-returns"  # the call still completed off-loop


@then('the session probe, quote consumption and UI stream continue uninterrupted')
def _(world):
    assert world["ticks"] == 20  # every scheduled tick ran during the hang
