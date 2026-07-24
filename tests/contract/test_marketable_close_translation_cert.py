"""Marketable-close ACL translation contract test (pytest -m contract, CERT
ONLY, DRY-RUN ONLY — nothing is ever placed).

LEX-05/STP-08a/CLS-01: proves against the REAL broker that the fixed
`marketable_close` translation validates — the exact shape every TPF/TPT/
CLS-01 manual close, STP-03b watchdog escalation, and LEX-05 fallback builds.

PROD dry-run probes 2026-07-24 (the facts this pins forever):
  * the broker's native "Marketable Limit" wire type REFUSES a client price
    (`order_must_omit_price`) — so the ACL must emit a plain LIMIT at the
    intent's bounded price, never the native marketable type and never a raw
    MARKET order (LEX-05: "marketable limit at the current bid ... never a
    raw market order");
  * prices are SIGNED net effect: a single-leg BUY at a POSITIVE price
    rejects `cant_buy_for_credit`; the same buy at a NEGATIVE price is
    accepted. The ACL signs all-buy orders negative (net debit).

The 2026-07-20 live incident this closes: TPF floor fired, every exit was
rejected with `order_must_omit_price`, positions stayed open.

The dry-run's "no existing position to close" style warning is EXPECTED on a
flat cert account and fine — validation passing (resp.order populated, no
price-shape rejection) is what this proves.

SKIPPED CLEANLY without cert credentials (never run as part of the offline
suite, and never run automatically by an agent — operator-triggered only):

    pytest -m contract tests/contract/test_marketable_close_translation_cert.py -s
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = pytest.mark.contract

pytest.importorskip("tastytrade")

from meic.adapters.tastytrade.adapter import TastytradeAdapter  # noqa: E402
from meic.application.order_intent import marketable_close  # noqa: E402

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
        # SKIPPED CLEANLY: no credentials means "not run", never a failure --
        # operator-triggered only, same convention as the chain cert tests.
        pytest.skip("no cert creds in .env -- marketable-close translation cert is operator-triggered only")
    return e


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def adapter(env):
    a = TastytradeAdapter(env["TT_CERT_PROVIDER_SECRET"], env["TT_CERT_REFRESH_TOKEN"], is_test=True)
    await a.connect(env.get("TT_CERT_ACCOUNT"))
    return a


@pytest.mark.asyncio(loop_scope="module")
async def test_marketable_close_translates_to_signed_limit_and_dry_runs_clean(adapter, env):
    """LEX-05/STP-08a/CLS-01: a `marketable_close` intent (single buy_to_close
    leg, unsigned premium 0.05) must translate to wire LIMIT @ -0.05 and pass
    the broker's own dry-run validation — the two rejections the 2026-07-24
    probes proved (`order_must_omit_price` for the native marketable type,
    `cant_buy_for_credit` for a positive-priced buy) must NOT occur."""
    from tastytrade import Session
    from tastytrade.instruments import NestedOptionChain
    from tastytrade.order import OrderType

    # Resolve a deep-OTM SPXW put off the LIVE chain, dte >= 1 (a next-day or
    # later expiration so the dry-run cannot brush against a same-day close).
    session = Session(env["TT_CERT_PROVIDER_SECRET"],
                      refresh_token=env["TT_CERT_REFRESH_TOKEN"], is_test=True)
    chain = (await NestedOptionChain.get(session, "SPXW"))[0]
    exp = next(e for e in sorted(chain.expirations, key=lambda e: e.expiration_date)
               if e.expiration_date >= date.today() + timedelta(days=1))
    strike = sorted(exp.strikes, key=lambda s: s.strike_price)[0]  # deepest OTM put

    intent = marketable_close(
        entry_id="cert-probe", right="P", contracts=1, price=D("0.05"),
        symbol=strike.put, expiration=exp.expiration_date)

    # 1) The ACL's own translation: plain LIMIT (never the native "Marketable
    #    Limit" wire type, never MARKET), price signed negative (net debit).
    new = await adapter._build_order(intent)
    assert new.order_type == OrderType.LIMIT
    assert new.price == D("-0.05")

    # 2) The real broker validates that exact shape — DRY-RUN ONLY, nothing
    #    placed. A rejection (order_must_omit_price / cant_buy_for_credit)
    #    raises out of place_order and fails this test; a flat-account
    #    "no existing position" warning is EXPECTED and fine.
    resp = await adapter._account.place_order(adapter._session, new, dry_run=True)
    assert resp.order is not None  # cert accepted the signed single-leg LIMIT close
