"""Live chain selection against the real feed — READ ONLY (pytest -m contract).

Places NO orders. Snapshots the real SPXW 0DTE chain over DXLink, then runs the
full selection pipeline and asserts the ONE invariant that matters:

    the selector either returns a sane Condor, or a NAMED skip reason.
    It must never return a Condor built from stale or holey data.

Run this at the market open to confirm the live path end-to-end before arming:

    pytest -m contract tests/contract/test_live_selection_cert.py -s
"""
from __future__ import annotations

import os
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]
pytest.importorskip("tastytrade")

from tastytrade import Session  # noqa: E402

from meic.adapters.dxlink.chain_snapshot import snapshot_chain  # noqa: E402
from meic.composition.live_selection import LiveCondorSelector, SelectionConfig  # noqa: E402
from meic.domain.chain import completeness_ok  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

KNOWN_SKIPS = {"data_unavailable", "incomplete_chain", "no_valid_strikes",
               "wing_unmarked", "insufficient_credit", "strike_collision"}


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


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def session() -> Session:
    e = _env()
    if not (e.get("TT_CERT_PROVIDER_SECRET") and e.get("TT_CERT_REFRESH_TOKEN")):
        pytest.fail("no cert creds in .env")
    return Session(e["TT_CERT_PROVIDER_SECRET"], refresh_token=e["TT_CERT_REFRESH_TOKEN"],
                   is_test=True)  # CERT ONLY


async def test_live_chain_snapshot_and_selection_read_only(session):
    cfg = SelectionConfig()

    snap = await snapshot_chain(session, max_age_seconds=5.0)

    put_complete = completeness_ok(snap.put_side, band_strikes=snap.put_band,
                                   completeness_pct=cfg.completeness_pct)
    call_complete = completeness_ok(snap.call_side, band_strikes=snap.call_band,
                                    completeness_pct=cfg.completeness_pct)
    print(f"\n--- LIVE CHAIN SNAPSHOT ({datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}) ---")
    print(f"  spot            : {snap.spot}")
    print(f"  expiration      : {snap.expiration}")
    print(f"  stale           : {snap.stale}")
    print(f"  put band/marked : {len(snap.put_band)} / {len(snap.put_side.marks)}  complete={put_complete}")
    print(f"  call band/marked: {len(snap.call_band)} / {len(snap.call_side.marks)} complete={call_complete}")

    # the snapshot itself must be structurally sound
    assert snap.spot > 0, "no spot from the index feed"
    assert snap.put_band and snap.call_band, "no strikes inside the ATM band"

    selector = LiveCondorSelector(snapshot_provider=lambda: _ready(snap), config=cfg)
    condor, reason = await selector(datetime.now(timezone.utc), 1)

    if condor is None:
        print(f"  SELECTION       : skipped -> {reason}")
        assert reason in KNOWN_SKIPS, f"unnamed skip reason {reason!r}"
        return  # a named skip is a correct outcome (closed / holey / thin market)

    print(f"  SELECTION       : put {condor.put_short} / call {condor.call_short} "
          f"| net credit {condor.mid_credit}")

    # If it DID select, the invariants must hold — no garbage condor, ever.
    assert reason is None
    assert not snap.stale, "selected on stale data"
    assert put_complete and call_complete, "selected on an incomplete chain"
    assert condor.put_short < snap.spot < condor.call_short, "shorts not OTM around spot"
    assert condor.put_short_mid >= cfg.min_short_premium
    assert condor.call_short_mid >= cfg.min_short_premium
    assert condor.mid_credit >= cfg.min_total_credit
    assert condor.mid_credit > 0


async def _ready(v):
    return v
