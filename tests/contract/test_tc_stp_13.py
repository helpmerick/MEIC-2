"""TC-STP-13 — STP-05a sandbox verification gate (contract tests).

Operator-triggered:  pytest -m contract
CERT ENVIRONMENT ONLY: Session(is_test=True) is hardcoded in the fixture and
the credentials fixture refuses any refresh token whose issuer is not the
cert/sandbox environment — locally, before any network call.

Credentials: .env at the repo root (gitignored, BOM-tolerant read per NFR-05):
    TT_CERT_PROVIDER_SECRET=...   # cert-env OAuth application secret
    TT_CERT_REFRESH_TOKEN=...     # cert-env OAuth refresh token
    TT_CERT_ACCOUNT=...           # optional; defaults to first account

Verification items (spec/README.md open items, STP-05a / STP-05 / TC-STP-08):
    1. single-leg SPXW stop-market support and acceptance
    2. stop trigger reference price (last trade vs NBBO/mark)
    3. stop persistence independent of the bot's session
    4. complex-order per-leg fill price allocation
    5. DXLink keepalive interval and quote-token lifetime

Every test dumps raw broker payloads to tests/contract/observations/*.json —
the evidence base for the written STP-05a findings report. Per STP-05a: if
single-leg option stops are unsupported, or the trigger source is last-trade
only, the build STOPS and the operator gets a spec-amendment conversation —
tests fail with actionable messages, they do not improvise workarounds.

SDK note: tastytrade v13 is fully async; tests run on a module-scoped event
loop so one session serves all five items.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="module")]

tastytrade = pytest.importorskip("tastytrade", reason="tastytrade SDK required (pip install -r backend/requirements.txt)")

from tastytrade import Account, Session  # noqa: E402
from tastytrade.instruments import NestedOptionChain, Option  # noqa: E402
from tastytrade.order import (  # noqa: E402
    NewOrder,
    OrderAction,
    OrderTimeInForce,
    OrderType,
)

ROOT = Path(__file__).resolve().parents[2]
OBS = Path(__file__).parent / "observations"


def _record(name: str, payload) -> None:
    OBS.mkdir(exist_ok=True)
    out = {"recorded_at": datetime.now(timezone.utc).isoformat(), "observation": payload}
    (OBS / f"{name}.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")


def _env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():  # NFR-05: BOM-tolerant
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update(os.environ)
    return env


def _jwt_issuer(token: str) -> str | None:
    """Local-only JWT payload decode (no network, no signature verification) —
    used solely to refuse non-cert tokens before any request is made."""
    import base64

    try:
        seg = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        return payload.get("iss")
    except Exception:
        return None


@pytest.fixture(scope="module")
def creds() -> tuple[str, str, str | None]:
    env = _env()
    secret, refresh = env.get("TT_CERT_PROVIDER_SECRET"), env.get("TT_CERT_REFRESH_TOKEN")
    if not (secret and refresh) or "REPLACE_ME" in (secret, refresh):
        pytest.fail(
            "STP-05a GATE: no cert credentials found. Fill .env at the repo root with "
            "TT_CERT_PROVIDER_SECRET and TT_CERT_REFRESH_TOKEN from a CERT-environment "
            "OAuth app (developer.tastytrade.com -> cert). Build MUST NOT proceed past "
            "this gate without the sandbox verification. (TC-STP-13)"
        )
    issuer = _jwt_issuer(refresh)
    if issuer is None or not ("cert" in issuer or "sandbox" in issuer):
        pytest.fail(
            f"STP-05a GATE: refresh token issuer is {issuer!r} — NOT a cert/sandbox "
            "token (cert issues iss=api.sandbox.tastyworks.com; production is "
            "api.tastytrade.com). PRODUCTION CREDENTIALS ARE REFUSED before any "
            "network call. Revoke the pasted grant, rotate the app secret, and "
            "supply CERT credentials. (TC-STP-13)"
        )
    return secret, refresh, env.get("TT_CERT_ACCOUNT")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def session(creds) -> Session:
    secret, refresh, _ = creds
    return Session(secret, refresh_token=refresh, is_test=True)  # CERT ONLY — never production


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def account(session, creds) -> Account:
    _, _, acct_number = creds
    accounts = await Account.get(session)
    if acct_number:
        matches = [a for a in accounts if a.account_number == acct_number]
        if not matches:
            pytest.fail(f"STP-05a: TT_CERT_ACCOUNT={acct_number!r} not among cert accounts {[a.account_number for a in accounts]}")
        return matches[0]
    if not accounts:
        pytest.fail("STP-05a: cert login OK but no cert account exists — create one in the cert environment.")
    return accounts[0]


def _next_live_expiration(chain):
    """Cert keeps expired expirations in the chain (orders on them are rejected
    with instruments_stopped_trading) — pick the first non-expired one."""
    from datetime import date

    for exp in sorted(chain.expirations, key=lambda e: e.expiration_date):
        if exp.expiration_date >= date.today():
            return exp
    pytest.fail("STP-05a: SPXW chain in cert has no non-expired expirations — escalate to operator.")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def spxw_far_otm_put(session) -> Option:
    """Next-live-expiration SPXW put, far OTM — cheap, and its stop trigger is unreachable."""
    chains = await NestedOptionChain.get(session, "SPXW")
    if not chains:
        pytest.fail("STP-05a: no SPXW chain in cert — verification item; escalate to operator (chain availability differs from production).")
    chain = chains[0]
    expiration = _next_live_expiration(chain)
    strikes = sorted(expiration.strikes, key=lambda s: s.strike_price)
    lowest = strikes[0]  # deepest OTM put available
    _record("00-chain-metadata", {
        "underlying": "SPXW",
        "expiration": str(expiration.expiration_date),
        "strike_count": len(strikes),
        "chosen_put_strike": str(lowest.strike_price),
        "put_symbol": lowest.put,
    })
    return await Option.get(session, lowest.put)


def _single_leg_stop(option: Option) -> NewOrder:
    return NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.STOP,
        stop_trigger=Decimal("0.05"),
        legs=[option.build_leg(Decimal(1), OrderAction.BUY_TO_OPEN)],
    )


# ---------------------------------------------------------------------------
# Item 1 — single-leg SPXW stop-market acceptance (THE build-blocking check)
# ---------------------------------------------------------------------------

async def test_tc_stp_13_item1_single_leg_spxw_stop_dry_run_acceptance(session, account, spxw_far_otm_put):
    """Dry-run a single-leg SPXW stop-market. Rejection as unsupported => STOP THE BUILD."""
    try:
        resp = await account.place_order(session, _single_leg_stop(spxw_far_otm_put), dry_run=True)
    except Exception as e:  # capture the raw rejection — it IS the finding
        _record("01-single-leg-stop-dry-run-REJECTED", {"exception": repr(e)})
        pytest.fail(
            f"STP-05a GATE FAILED: cert rejected a single-leg SPXW stop-market order: {e!r}. "
            "BUILD MUST NOT PROCEED — this is a spec-amendment conversation with the operator "
            "(e.g. bot-side watchdog as secondary trigger layer). See "
            "tests/contract/observations/01-single-leg-stop-dry-run-REJECTED.json (TC-STP-13)"
        )
    _record("01-single-leg-stop-dry-run-ACCEPTED", {
        "order": resp.order.model_dump() if resp.order else None,
        "warnings": [w.model_dump() for w in (resp.warnings or [])],
        "buying_power_effect": resp.buying_power_effect.model_dump() if resp.buying_power_effect else None,
    })
    assert resp.order is not None, "dry-run returned no order object — inspect observation JSON"


async def test_tc_stp_13_item1_3_stop_rests_and_survives_session_death(creds, session, account, spxw_far_otm_put):
    """Place a REAL cert resting stop (trigger far from market), then prove it is
    visible from a brand-new session (STP-05, TC-STP-08 basis). Cancels at the end."""
    placed = await account.place_order(session, _single_leg_stop(spxw_far_otm_put), dry_run=False)
    order_id = placed.order.id
    _record("03-resting-stop-placed", placed.order.model_dump())

    secret, refresh, _ = creds
    second_session = Session(secret, refresh_token=refresh, is_test=True)  # fresh session, CERT
    try:
        fresh_account = await Account.get(second_session, account.account_number)
        live = await fresh_account.get_live_orders(second_session)
        survivors = [o for o in live if o.id == order_id]
        _record("03-resting-stop-second-session-view", {
            "order_id": order_id,
            "visible_from_new_session": bool(survivors),
            "status": str(survivors[0].status) if survivors else None,
            "received_at": str(survivors[0].received_at) if survivors else None,
            "full": survivors[0].model_dump() if survivors else None,
        })
        assert survivors, (
            "STP-05a/STP-05: resting stop NOT visible from an independent session — "
            "stop persistence claim fails; escalate to operator. (TC-STP-08 basis)"
        )
        assert str(survivors[0].status).lower().split(".")[-1] in ("live", "received"), (
            f"stop found but not working: status={survivors[0].status!r} — inspect observation JSON"
        )
    finally:
        try:
            fresh_account = await Account.get(second_session, account.account_number)
            await fresh_account.delete_order(second_session, order_id)
        except Exception as e:
            _record("03-cleanup-cancel-failed", {"order_id": order_id, "exception": repr(e)})


# ---------------------------------------------------------------------------
# Item 2 — trigger reference price (last trade vs NBBO/mark)
# ---------------------------------------------------------------------------

async def test_tc_stp_13_item2_trigger_reference_price_evidence(session, account, spxw_far_otm_put):
    """Cert cannot force prints on demand, so this test captures every trigger-
    related field the API exposes on a placed stop. Determination logic:
    - explicit trigger-source field => record it verbatim
    - nothing exposed => 'indeterminate-in-cert' => operator conversation
      (per STP-05a: last-trade-ONLY confirmed => build stops; indeterminate
      => escalate, do not assume)."""
    resp = await account.place_order(session, _single_leg_stop(spxw_far_otm_put), dry_run=True)
    dumped = resp.order.model_dump() if resp.order else {}
    trigger_fields = {k: str(v) for k, v in dumped.items() if "trigger" in k.lower() or "stop" in k.lower()}
    _record("02-trigger-source-evidence", {
        "trigger_related_fields": trigger_fields,
        "full_order_payload": dumped,
        "determination": "see findings report — cert-side evidence only; "
                         "explicit trigger-source field absent means INDETERMINATE-IN-CERT",
    })
    assert trigger_fields, "no trigger-related fields captured — inspect full payload in observation JSON"


# ---------------------------------------------------------------------------
# Item 4 — complex-order per-leg fill price allocation
# ---------------------------------------------------------------------------

async def test_tc_stp_13_item4_complex_order_per_leg_allocation(session, account):
    """Place a marketable 4-leg SPXW iron condor in cert; record how the broker
    allocates per-leg fill prices against the net (STP-02 per_side caveat)."""
    chains = await NestedOptionChain.get(session, "SPXW")
    chain = chains[0]
    expiration = _next_live_expiration(chain)
    strikes = sorted(expiration.strikes, key=lambda s: s.strike_price)
    if len(strikes) < 8:
        pytest.fail(f"STP-05a item 4: only {len(strikes)} SPXW strikes in cert — cannot build a condor; escalate.")
    n = len(strikes)
    put_short, put_long = strikes[n // 4], strikes[n // 4 - 1]
    call_short, call_long = strikes[3 * n // 4], strikes[3 * n // 4 + 1]
    legs = [
        (await Option.get(session, put_long.put)).build_leg(Decimal(1), OrderAction.BUY_TO_OPEN),
        (await Option.get(session, put_short.put)).build_leg(Decimal(1), OrderAction.SELL_TO_OPEN),
        (await Option.get(session, call_short.call)).build_leg(Decimal(1), OrderAction.SELL_TO_OPEN),
        (await Option.get(session, call_long.call)).build_leg(Decimal(1), OrderAction.BUY_TO_OPEN),
    ]
    order = NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        price=Decimal("0.05"),  # near-zero credit demand => marketable in cert
        legs=legs,
    )
    placed = await account.place_order(session, order, dry_run=False)
    _record("04-condor-placed", placed.order.model_dump())
    fills = None
    for _ in range(12):  # up to ~60s for the cert fill simulator
        await asyncio.sleep(5)
        current = await account.get_order(session, placed.order.id)
        if str(current.status).lower().endswith("filled"):
            fills = current.model_dump()
            break
    _record("04-condor-fill-allocation", {
        "filled": fills is not None,
        "order_final": fills or (await account.get_order(session, placed.order.id)).model_dump(),
        "note": "per-leg fill prices vs net: evidence for the STP-02 per_side allocation caveat",
    })
    if fills is None:
        try:
            await account.delete_order(session, placed.order.id)
        finally:
            pytest.fail("STP-05a item 4: condor did not fill in cert within 60s — cert fill realism limit; recorded, escalate in findings.")


# ---------------------------------------------------------------------------
# Item 5 — DXLink keepalive + quote-token lifetime
# ---------------------------------------------------------------------------

async def test_tc_stp_13_item5_dxlink_keepalive_and_quote_token(session, caplog):
    """The DXLink SETUP frame carries the negotiated keepalive; the SDK logs it
    at DEBUG. Capture that plus any token-lifetime metadata the SDK exposes."""
    from tastytrade import DXLinkStreamer

    caplog.set_level(logging.DEBUG, logger="tastytrade")
    async with DXLinkStreamer(session) as streamer:
        await asyncio.sleep(1)  # let SETUP/AUTH frames arrive and log
        streamer_attrs = {
            a: str(getattr(streamer, a))
            for a in dir(streamer)
            if not a.startswith("__") and any(w in a.lower() for w in ("keepalive", "timeout", "token", "url", "auth"))
            and not callable(getattr(streamer, a, None))
        }
    setup_frames = [r.message for r in caplog.records if "SETUP" in r.message or "keepalive" in r.message.lower()]
    token_log_lines = [r.message for r in caplog.records if "token" in r.message.lower() or "expires" in r.message.lower()]
    _record("05-dxlink-keepalive-and-token", {
        "setup_frames": setup_frames,
        "token_log_lines": token_log_lines,
        "streamer_attrs": streamer_attrs,
        "note": "spec expectation ~24h quote token (doc 05 NFR-04); session OAuth token separately ~900s per refresh log",
    })
    assert setup_frames or streamer_attrs, "no keepalive evidence captured — inspect SDK logging config"
