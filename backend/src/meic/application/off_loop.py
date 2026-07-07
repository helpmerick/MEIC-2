"""Off-loop serialized execution — NFR-01.

Every synchronous SDK/REST call runs on ONE dedicated worker thread — off the
event loop and serialized, so the shared HTTP session is never used
concurrently. A stalled call delays only itself; the scheduler, streams, UI and
process managers keep running.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class OffLoopExecutor:
    def __init__(self) -> None:
        # max_workers=1 is the serialization guarantee: two SDK calls never run
        # concurrently against the shared session.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="meic-sdk")

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, lambda: fn(*args))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
