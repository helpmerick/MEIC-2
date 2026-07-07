"""Runtime NFR TCs: NFR-01 (off-loop), NFR-02 (health loop), NFR-03 (timeouts),
NFR-05 (file integrity)."""
import asyncio
import time
from pathlib import Path

import pytest

from meic.adapters.persistence.file_integrity import (
    IntegrityGuard,
    hash_file,
    read_text_bom_tolerant,
)
from meic.application.off_loop import OffLoopExecutor
from meic.application.session_health import health_actions
from meic.application.timeouts import HTTP_TIMEOUTS, run_warmup, with_cap


# --- NFR-01: off-loop, serialized --------------------------------------------

def test_tc_nfr_01_blocking_call_does_not_stall_the_loop_and_serializes():
    """TC-NFR-01: a stalled synchronous call delays only itself — the event
    loop keeps running — and calls are serialized (never concurrent)."""
    async def scenario():
        ex = OffLoopExecutor()
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(20):
                ticks += 1
                await asyncio.sleep(0.005)

        def slow_sdk_call():
            time.sleep(0.06)  # a blocking SDK call
            return "ok"

        t = asyncio.ensure_future(ticker())
        result = await ex.run(slow_sdk_call)   # off-loop — must NOT freeze the ticker
        await t
        ex.shutdown()
        assert result == "ok"
        assert ticks >= 5                       # the loop kept running during the blocking call

        # serialization: two off-loop calls never overlap on the shared worker
        active = 0
        overlap = False

        def guarded():
            nonlocal active, overlap
            active += 1
            if active > 1:
                overlap = True
            time.sleep(0.02)
            active -= 1

        ex2 = OffLoopExecutor()
        await asyncio.gather(ex2.run(guarded), ex2.run(guarded))
        ex2.shutdown()
        assert overlap is False                 # single worker -> serialized

    asyncio.run(scenario())


# --- NFR-02: session health loop ---------------------------------------------

def test_tc_nfr_02_probe_refresh_market_hours_only():
    """TC-NFR-02: probes run during market hours only (zero outside); a token
    error forces an immediate refresh; proactive refresh on its interval."""
    # outside market hours: nothing, ever
    assert health_actions(market_open=False, seconds_since_probe=999, seconds_since_refresh=999,
                          probe_interval=60, refresh_interval=300) == set()
    # due probe, not yet due refresh
    assert health_actions(market_open=True, seconds_since_probe=61, seconds_since_refresh=100,
                          probe_interval=60, refresh_interval=300) == {"probe"}
    # a token error forces refresh immediately
    assert "refresh" in health_actions(market_open=True, seconds_since_probe=1, seconds_since_refresh=1,
                                       probe_interval=60, refresh_interval=300, probe_error=True)
    # proactive refresh on interval
    assert "refresh" in health_actions(market_open=True, seconds_since_probe=1, seconds_since_refresh=301,
                                       probe_interval=60, refresh_interval=300)


# --- NFR-03: timeouts ---------------------------------------------------------

def test_tc_nfr_03_warmup_capped_entry_still_fires():
    """TC-NFR-03: a warm-up primed against a black-hole aborts at its hard cap;
    the entry proceeds (gates decide normally). Every HTTP client carries
    explicit timeouts."""
    assert set(HTTP_TIMEOUTS) == {"connect", "read", "write", "pool"}

    async def scenario():
        async def black_hole():
            await asyncio.sleep(3600)  # never returns

        start = asyncio.get_event_loop().time()
        completed, result = await run_warmup(black_hole(), cap_seconds=0.05)
        elapsed = asyncio.get_event_loop().time() - start
        assert completed is False and result is None   # capped, not hung
        assert elapsed < 1.0                            # aborted at the cap, entry can fire

        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await with_cap(black_hole(), cap_seconds=0.05)

    asyncio.run(scenario())


# --- NFR-05: file integrity ---------------------------------------------------

def test_tc_nfr_05_bom_tolerant_and_hash_guard(tmp_path):
    """TC-NFR-05: a BOM-prefixed .env loads with key names intact; a file whose
    hash changed outside the bot's writes blocks arming (named); a truncated
    file is refused, never silently defaulted."""
    env = tmp_path / ".env"
    env.write_bytes("﻿TT_CERT_PROVIDER_SECRET=abc\n".encode("utf-8"))  # BOM-prefixed
    text = read_text_bom_tolerant(env)
    assert text.splitlines()[0] == "TT_CERT_PROVIDER_SECRET=abc"  # BOM stripped, key intact

    cfg = tmp_path / "config.yml"
    cfg.write_text("stop_loss_pct: 95\n", encoding="utf-8")
    recorded = {"config.yml": hash_file(cfg)}
    guard = IntegrityGuard(recorded)
    assert guard.may_arm({"config.yml": hash_file(cfg)}) is True  # unchanged -> may arm

    cfg.write_text("stop_loss_pct: 300\n", encoding="utf-8")  # changed outside the bot
    current = {"config.yml": hash_file(cfg)}
    assert guard.may_arm(current) is False                       # refuse to arm
    assert guard.changed_files(current) == ["config.yml"]        # names the file

    cfg.write_text("", encoding="utf-8")  # truncated
    assert guard.may_arm({"config.yml": hash_file(cfg)}) is False  # refused, not defaulted
