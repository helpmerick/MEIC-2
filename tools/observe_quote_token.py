#!/usr/bin/env python3
"""DXLink quote-token boundary observation (Phase-5 assumption 9).

Holds a cert DXLink connection open across a full day (default 25 h, to cross
the ~24 h streaming-token boundary) and logs the token lifecycle + keepalive +
quote-flow status to a JSONL file. This is the full-day observation the
STP-05a findings flagged as unresolved: does the ~24 h `/api-quote-tokens`
streaming token expire, silently renew, or drop the stream at the boundary?

CERT ONLY: Session(is_test=True); the issuer guard refuses a non-cert token
before connecting. Unattended-safe: reconnects on failure, never trades.

Run:  python tools/observe_quote_token.py --hours 25
Output: tools/observations/quote-token-YYYYMMDD-HHMMSS.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "tools" / "observations"


def _env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _issuer(token: str) -> str | None:
    try:
        seg = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))).get("iss")
    except Exception:
        return None


def _log(fh, event: str, **fields) -> None:
    rec = {"at_utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    fh.write(json.dumps(rec, default=str) + "\n")
    fh.flush()
    print(rec["at_utc"], event, {k: v for k, v in fields.items() if k != "raw"})


async def observe(hours: float) -> int:
    env = _env()
    secret, refresh = env.get("TT_CERT_PROVIDER_SECRET"), env.get("TT_CERT_REFRESH_TOKEN")
    if not (secret and refresh):
        print("no cert creds in .env", file=sys.stderr)
        return 1
    issuer = _issuer(refresh)
    if not issuer or not ("cert" in issuer or "sandbox" in issuer):
        print(f"refusing non-cert token (issuer {issuer!r})", file=sys.stderr)
        return 1

    from tastytrade import DXLinkStreamer, Session
    from tastytrade.dxfeed import Quote

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"quote-token-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    deadline = asyncio.get_event_loop().time() + hours * 3600

    with out.open("w", encoding="utf-8") as fh:
        _log(fh, "start", hours=hours, issuer=issuer)
        session = Session(secret, refresh_token=refresh, is_test=True)
        # capture streamer-token metadata at day start (the ~24h token)
        for attr in ("streamer_token", "dxlink_url", "streamer_expiration", "streamer_headers"):
            if hasattr(session, attr):
                val = getattr(session, attr)
                _log(fh, "session_attr", name=attr, present=True,
                     value=("<redacted>" if "token" in attr else str(val)))

        reconnects = 0
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with DXLinkStreamer(session) as streamer:
                    _log(fh, "streamer_connected", reconnects=reconnects)
                    await streamer.subscribe(Quote, ["SPX"])
                    last_tick = asyncio.get_event_loop().time()
                    while asyncio.get_event_loop().time() < deadline:
                        try:
                            q = await asyncio.wait_for(streamer.get_event(Quote), timeout=300)
                            now = asyncio.get_event_loop().time()
                            # heartbeat every ~5 min of flow
                            if now - last_tick >= 300:
                                _log(fh, "quotes_flowing", symbol=getattr(q, "event_symbol", "SPX"))
                                last_tick = now
                        except asyncio.TimeoutError:
                            _log(fh, "quote_silence_300s")  # possible token-boundary signal
            except Exception as e:
                reconnects += 1
                _log(fh, "streamer_error", error=repr(e), reconnects=reconnects)
                await asyncio.sleep(min(30, 2 ** min(reconnects, 5)))
        _log(fh, "done", reconnects=reconnects, output=str(out))
    print(f"\nobservation written to {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=25.0, help="how long to hold the connection")
    args = ap.parse_args()
    return asyncio.run(observe(args.hours))


if __name__ == "__main__":
    sys.exit(main())
