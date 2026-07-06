"""TastytradeAdapter live-wiring contract tests (pytest -m contract, CERT ONLY).

Proves the BrokerGateway wiring against cert: connect, positions read, working
orders read, and a dry-run of a single-leg SPXW Day-TIF stop (assumptions
1/2/7). Economics stay with the fakes/SIM; this only proves the ACL talks to
the broker correctly.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = pytest.mark.contract

pytest.importorskip("tastytrade")

from meic.adapters.tastytrade.adapter import TastytradeAdapter  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


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
        pytest.fail("no cert creds in .env — refresh the cert grant (see the wiring session)")
    return e


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def adapter(env):
    a = TastytradeAdapter(env["TT_CERT_PROVIDER_SECRET"], env["TT_CERT_REFRESH_TOKEN"], is_test=True)
    await a.connect(env.get("TT_CERT_ACCOUNT"))
    return a


@pytest.mark.asyncio(loop_scope="module")
async def test_positions_read(adapter):
    positions = await adapter.positions()
    assert isinstance(positions, list)  # cert may be flat — the read path is what we prove


@pytest.mark.asyncio(loop_scope="module")
async def test_working_orders_read(adapter):
    orders = await adapter.working_orders()
    assert isinstance(orders, list)


@pytest.mark.asyncio(loop_scope="module")
async def test_single_leg_spxw_day_stop_dry_run_accepted(adapter, env):
    """Assumption 1/2/7: a Day-TIF single-leg SPXW stop dry-runs clean against
    cert. Resolves a live far-OTM put symbol first."""
    from tastytrade import Session
    from tastytrade.instruments import NestedOptionChain

    session = Session(env["TT_CERT_PROVIDER_SECRET"], refresh_token=env["TT_CERT_REFRESH_TOKEN"], is_test=True)
    chain = (await NestedOptionChain.get(session, "SPXW"))[0]
    exp = next(e for e in sorted(chain.expirations, key=lambda e: e.expiration_date)
               if e.expiration_date >= date.today())
    strike = sorted(exp.strikes, key=lambda s: s.strike_price)[0]  # deepest OTM put

    intent = {"order_type": "stop_market", "tif": "Day", "stop_trigger": D("0.05"),
              "legs": [{"symbol": strike.put, "action": "buy_to_open", "qty": 1}]}
    resp = await adapter.dry_run(intent)
    assert resp.order is not None  # cert accepted the single-leg Day-TIF stop


@pytest.mark.asyncio(loop_scope="module")
async def test_gtc_option_stop_rejected_client_side(adapter, env):
    """Assumption 2: a GTC option stop is refused BEFORE hitting the broker."""
    from tastytrade import Session
    from tastytrade.instruments import NestedOptionChain

    session = Session(env["TT_CERT_PROVIDER_SECRET"], refresh_token=env["TT_CERT_REFRESH_TOKEN"], is_test=True)
    chain = (await NestedOptionChain.get(session, "SPXW"))[0]
    exp = next(e for e in sorted(chain.expirations, key=lambda e: e.expiration_date)
               if e.expiration_date >= date.today())
    strike = sorted(exp.strikes, key=lambda s: s.strike_price)[0]
    intent = {"order_type": "stop_market", "tif": "GTC", "stop_trigger": D("0.05"),
              "legs": [{"symbol": strike.put, "action": "buy_to_open", "qty": 1}]}
    with pytest.raises(ValueError):  # client-side reject, never sent
        await adapter.dry_run(intent)
