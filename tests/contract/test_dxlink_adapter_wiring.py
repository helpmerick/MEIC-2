"""DXLinkAdapter live-wiring contract tests (pytest -m contract, CERT ONLY).

Proves the MarketDataFeed wiring against cert: a REST chain snapshot, and a
staleness-stamped streaming quote. Runs only after the quote-token observation
freed the DXLink socket.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = pytest.mark.contract

pytest.importorskip("tastytrade")

from meic.adapters.dxlink.adapter import DXLinkAdapter  # noqa: E402
from meic.domain.staleness import StampedQuote  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


class _RealClock:
    def now(self):
        return datetime.now()


def _env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update(os.environ)
    return env


@pytest.fixture(scope="module")
def env():
    e = _env()
    if not (e.get("TT_CERT_PROVIDER_SECRET") and e.get("TT_CERT_REFRESH_TOKEN")):
        pytest.fail("no cert creds in .env")
    return e


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def adapter(env):
    from tastytrade import Session
    session = Session(env["TT_CERT_PROVIDER_SECRET"], refresh_token=env["TT_CERT_REFRESH_TOKEN"], is_test=True)
    return DXLinkAdapter(session, _RealClock())


@pytest.mark.asyncio(loop_scope="module")
async def test_chain_snapshot(adapter):
    chain = await adapter.chain("SPXW", "")
    assert chain is not None and getattr(chain, "expirations", None)


@pytest.mark.asyncio(loop_scope="module")
async def test_streaming_quote_is_stamped(adapter):
    import asyncio

    async def first():
        async for q in adapter.quotes(["SPX"]):
            return q

    q = await asyncio.wait_for(first(), timeout=30)
    assert isinstance(q, StampedQuote)
    assert q.symbol and q.stamped_at is not None  # DAT-02 stamp present
