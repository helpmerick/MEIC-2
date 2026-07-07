"""Timeouts — NFR-03. No network operation may wait unboundedly.

Explicit connect/read/write/pool timeouts on every HTTP client, and the ENT-08
warm-up runs under a hard wall-clock cap so a stalled prime can never run into
the fire window.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar

T = TypeVar("T")

# config.http_timeout_seconds default 10 — applied to every HTTP client.
HTTP_TIMEOUTS = {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}


class WarmupCapped(Exception):
    """The ENT-08 warm-up hit its hard cap; the entry fires anyway (ENT-08.4)."""


async def with_cap(coro: Awaitable[T], *, cap_seconds: float) -> T:
    """Run a coroutine under a hard wall-clock cap (NFR-03)."""
    return await asyncio.wait_for(coro, timeout=cap_seconds)


async def run_warmup(coro: Awaitable[T], *, cap_seconds: float) -> tuple[bool, T | None]:
    """ENT-08 warm-up under its cap: returns (completed, result). A stall aborts
    at the cap and never delays the scheduled entry."""
    try:
        return True, await with_cap(coro, cap_seconds=cap_seconds)
    except (asyncio.TimeoutError, TimeoutError):
        return False, None  # capped — the ENT-03 gates decide the entry as normal
