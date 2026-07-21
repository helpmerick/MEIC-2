"""RUT chain snapshot — READ ONLY (pytest -m contract), UND-01/UND-04/UND-06.

Mirrors test_live_selection_cert.py's SPX cert exactly, scoped to RUT — the
per-underlying contract test UND-06 calls for: "proves fills, symbology,
[and] multiplier P&L" for the underlying profile being brought up. This file
covers the symbology/chain half (a real RUTW 0DTE chain snapshot over
DXLink, via the profile's own `option_root`/`index_symbol` — never a
hand-typed "RUTW" literal); fills/multiplier P&L are proven at the domain
level by tests/application/test_tc_und_01.py and exercised end-to-end in
paper mode by the SimulatedBroker wiring.

Places NO orders — read-only, like its SPX sibling.

SKIPPED CLEANLY without cert credentials (never run as part of the offline
suite, and never run automatically by this agent):

    pytest -m contract tests/contract/test_rut_chain_cert.py -s
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]
pytest.importorskip("tastytrade")

from tastytrade import Session  # noqa: E402

from meic.adapters.dxlink.chain_snapshot import snapshot_chain  # noqa: E402
from meic.domain.underlying import PROFILES  # noqa: E402

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


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def session() -> Session:
    e = _env()
    if not (e.get("TT_CERT_PROVIDER_SECRET") and e.get("TT_CERT_REFRESH_TOKEN")):
        # SKIPPED CLEANLY (H): no credentials means "not run", never a failure —
        # this test proves live RUTW symbology when the operator chooses to run
        # it against the sandbox, not on every offline pass.
        pytest.skip("no cert creds in .env -- RUT chain cert is operator-triggered only")
    return Session(e["TT_CERT_PROVIDER_SECRET"], refresh_token=e["TT_CERT_REFRESH_TOKEN"],
                   is_test=True)  # CERT ONLY


async def test_rut_chain_snapshot_read_only(session):
    """UND-01/UND-04/UND-06: the RUT profile's `option_root`/`index_symbol`
    drive a real RUTW 0DTE chain snapshot — the live proof that RUTW
    streamer symbols follow the SAME `.ROOTYYMMDD…` shape SPXW already does
    (see domain/underlying.py's RUT profile citation). Never places an
    order."""
    profile = PROFILES["RUT"]
    assert profile.enabled, "RUT must be a tradable profile this phase (UND-06 build order)"

    snap = await snapshot_chain(session, underlying=profile.option_root,
                                index_symbol=profile.index_symbol, max_age_seconds=5.0)

    print("\n--- RUT (RUTW) CHAIN SNAPSHOT ---")
    print(f"  spot                   : {snap.spot}")
    # FIX-5 (cert triage 2026-07-21): RUT's index Quote publishes NaN
    # bid/ask, so its spot is expected via the Trade-last fallback --
    # `spot_source` records which dxfeed event actually delivered it.
    print(f"  spot_source            : {snap.spot_source}")
    print(f"  source underlying      : {snap.underlying}")
    print(f"  expiration             : {snap.expiration}")
    print(f"  stale                  : {snap.stale}")
    print(f"  put subscribed/marked  : {len(snap.put_band)} / {len(snap.put_side.marks)}")
    print(f"  call subscribed/marked : {len(snap.call_band)} / {len(snap.call_side.marks)}")

    # The snapshot itself must be structurally sound — the same bar
    # test_live_selection_cert.py holds SPXW to.
    assert snap.spot > 0, "no spot from the RUT index feed (Quote mid AND Trade last both absent)"
    assert snap.spot_source in ("quote_mid", "trade_last")
    assert snap.underlying == "RUT"   # UND-04 defense-in-depth stamp
    assert snap.put_band and snap.call_band, "no RUTW strikes subscribed near spot"
