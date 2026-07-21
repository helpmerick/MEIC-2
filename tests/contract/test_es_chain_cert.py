"""/ES futures-option chain snapshot — READ ONLY (pytest -m contract),
UND-01/UND-04 (Stage 1: adapter path only).

Mirrors test_rut_chain_cert.py, scoped to /ES's FUTURES-OPTION fetch path
(NestedFutureOptionChain, front-future spot) rather than the cash-index
NestedOptionChain path RUT/SPX use. Proves: a positive front-future spot, a
strike harvest with streamer symbols off the nearest live expiration, and
records `spot_source`.

/ES stays `enabled=False` this stage (UND-03's F3 force-close + enable is
Stage 2) -- this test does NOT assert `profile.enabled` the way the RUT cert
test does; it proves the adapter PATH works against the real broker
independent of whether the profile is tradeable yet.

Places NO orders — read-only, like its RUT/SPX siblings.

SKIPPED CLEANLY without cert credentials (never run as part of the offline
suite, and never run automatically by this agent):

    pytest -m contract tests/contract/test_es_chain_cert.py -s
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
        # SKIPPED CLEANLY (H): no credentials means "not run", never a failure --
        # operator-triggered only, same convention as the RUT/SPX cert tests.
        pytest.skip("no cert creds in .env -- /ES chain cert is operator-triggered only")
    return Session(e["TT_CERT_PROVIDER_SECRET"], refresh_token=e["TT_CERT_REFRESH_TOKEN"],
                   is_test=True)  # CERT ONLY


async def test_es_futures_option_chain_snapshot_read_only(session):
    """UND-01/UND-04 (/ES Stage 1): the /ES profile's `option_root`
    (the futures-option chain-fetch key, "/ES") drives a real futures-option
    0DTE chain snapshot — proving the front-future spot resolution and
    strike/streamer symbology against the live broker chain. Never places an
    order. Does NOT require `profile.enabled` -- /ES stays non-tradeable
    this stage (Stage 2 flips it)."""
    profile = PROFILES["/ES"]
    assert profile.instrument_class == "futures_option"

    snap = await snapshot_chain(session, underlying=profile.option_root,
                                index_symbol=profile.index_symbol, max_age_seconds=5.0)

    print("\n--- /ES (futures-option) CHAIN SNAPSHOT ---")
    print(f"  spot                   : {snap.spot}")
    print(f"  spot_source            : {snap.spot_source}")
    print(f"  source underlying      : {snap.underlying}")
    print(f"  expiration             : {snap.expiration}")
    print(f"  stale                  : {snap.stale}")
    print(f"  put subscribed/marked  : {len(snap.put_band)} / {len(snap.put_side.marks)}")
    print(f"  call subscribed/marked : {len(snap.call_band)} / {len(snap.call_side.marks)}")

    # The snapshot itself must be structurally sound — the same bar
    # test_rut_chain_cert.py / test_live_selection_cert.py hold SPXW/RUTW to.
    assert snap.spot > 0, "no spot from the /ES front future (Quote mid AND Trade last both absent)"
    assert snap.spot_source in ("quote_mid", "trade_last")
    assert snap.underlying == "/ES"   # UND-04 defense-in-depth stamp
    assert snap.put_band and snap.call_band, "no /ES futures-option strikes subscribed near spot"
