"""FastAPI control panel — commands + read model (doc 05 §7), NFR-06 secured.

The frontend holds NO trading logic (UI-03): this exposes read-model
projections and command endpoints; all validation is server-side. Security
(NFR-06): localhost-default bind; any MUTATING request (and WS upgrade) with a
foreign Origin is rejected (403); a token, optional on localhost, is required
(and enforced) whenever set. The bot never exposes account-mutating routes
unauthenticated.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from meic.application.persistent_state import PersistentState
from meic.application.preflight import run_preflight
from meic.application.schedule_service import ScheduleService
from meic.config.validation import ConfigRejected, validate_config
from meic.config.stop_basis import StopBasisRejected
from meic.domain.projection import day_report, fold


def _strike_from_symbol(symbol: str) -> str:
    """The OCC symbol's last 8 chars are the strike x1000 (adapters/occ.py) —
    the reverse of `occ_symbol`, for card display only (never re-used for orders:
    ORD-09's stops/LEX/closes all take the leg's own recorded `.symbol`)."""
    return str(Decimal(symbol[-8:]) / 1000)


def _card_legs(legs) -> list[dict[str, Any]]:
    """FEATURE 2 (v1.46 card): per-side strikes + the broker-allocated fill price,
    Decimals kept as strings in JSON. `qty` rides along so the live-P&L enricher
    (server.py) can size its estimate without re-deriving contracts elsewhere."""
    return [{
        "side": leg.side, "role": leg.role,
        "strike": _strike_from_symbol(leg.symbol),
        "price": None if leg.price is None else str(leg.price),
        "qty": leg.qty,
    } for leg in legs]


def _premium_received(legs) -> dict[str, str | None]:
    """FEATURE 2: short.price - long.price per side, only when BOTH prices are
    known (paper/simulated fills carry no allocation — null is the honest answer,
    never a fabricated number)."""
    by_side: dict[str, dict[str, Any]] = {"PUT": {}, "CALL": {}}
    for leg in legs:
        by_side.setdefault(leg.side, {})[leg.role] = leg
    out: dict[str, str | None] = {}
    for side in ("PUT", "CALL"):
        short, long_ = by_side[side].get("short"), by_side[side].get("long")
        if short is not None and long_ is not None and short.price is not None and long_.price is not None:
            out[side] = str(short.price - long_.price)
        else:
            out[side] = None
    return out


_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_loopback(host_header: str) -> bool:
    """True iff the Host names this machine's loopback interface (port ignored)."""
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header
    return host.lower() in _LOOPBACK


def origin_allowed(origin: str | None, *, scheme: str, host: str, panel_origin: str) -> bool:
    """NFR-06 (2): a mutating request's Origin must be the panel's OWN host.

    A hostile page can fire requests at localhost from inside the operator's
    browser, so a foreign Origin is refused. But the panel is served BY this app,
    which means the request's own origin IS the panel's own host — including the
    port. Comparing against a portless constant refused the panel's own Save
    button while letting nothing else through: security theatre that only ever
    fired on the legitimate user.

    Allowed:
      * no Origin at all — not a browser (the documented curl fallback, UI-09/17)
      * the configured `panel_origin`, exactly (a reverse proxy, say)
      * this request's own origin, provided the Host is loopback

    The loopback condition is what stops DNS rebinding: an attacker who resolves
    their own domain to 127.0.0.1 sends Origin == Host, but that Host is theirs,
    not loopback, so it is refused.
    """
    if origin is None:
        return True
    if origin == panel_origin:
        return True
    return origin == f"{scheme}://{host}" and _is_loopback(host)


def _describe(ev: Any) -> dict[str, str] | None:
    """Turn a domain event into one activity-feed line (icon, label, entry,
    detail). Presentation only — the UI renders whatever it's given."""
    name = type(ev).__name__
    entry = getattr(ev, "entry_id", "") or ""
    table: dict[str, tuple[str, str]] = {
        "DayArmed": ("🟢", "Day armed"),
        "EntryWindowOpened": ("⏱️", "Entry window opened"),
        "CondorProposed": ("📐", "Condor proposed"),
        "CondorFilled": ("✅", "Entry filled"),
        "StopPlaced": ("🛡️", "Stop placed"),
        "StopConfirmed": ("🔒", "Stop confirmed"),
        "ShortStopped": ("🔴", "Short stopped out"),
        "LongSaleStarted": ("↩️", "LEX recovery started"),
        "LongSold": ("💰", "Long sold (LEX)"),
        "SideClosed": ("➖", "Side closed"),
        "SideExpired": ("⌛", "Side expired worthless"),
        "EntryClosed": ("📕", "Entry closed"),
        "EntrySkipped": ("⚠️", "Entry skipped"),
        "WatchdogEscalated": ("🚨", "Watchdog escalated"),
        "DayCompleted": ("🏁", "Day completed"),
        "ModeSwitchStaged": ("🔀", "Mode switch staged"),
    }
    if name not in table:
        return None
    icon, label = table[name]
    bits = []
    for attr in ("side", "initiator", "reason", "action", "target", "effective"):
        v = getattr(ev, attr, None)
        if v:
            bits.append(str(v))
    for attr, sym in (("net_credit", "cr "), ("fill", "@ "), ("recovery", "rec ")):
        v = getattr(ev, attr, None)
        if v is not None and str(v) != "0":
            bits.append(f"{sym}${v}")
    return {"icon": icon, "label": label, "entry": entry, "detail": " · ".join(bits)}


def create_app(
    state: PersistentState,
    events: list,
    *,
    api_token: str | None = None,
    panel_origin: str = "http://127.0.0.1",
    commands: Any = None,
    entries_enricher: Any = None,  # FEATURE 3: optional (list[dict]) -> list[dict] hook,
    # e.g. live's snapshot-derived P/L (server.py); paper passes None (unchanged).
) -> FastAPI:
    app = FastAPI(title="MEIC control panel")

    @app.middleware("http")
    async def security(request: Request, call_next):
        mutating = request.method in ("POST", "PUT", "DELETE", "PATCH")
        if mutating:
            # NFR-06 (2): reject a foreign Origin even on localhost
            if not origin_allowed(request.headers.get("origin"),
                                  scheme=request.url.scheme,
                                  host=request.headers.get("host", ""),
                                  panel_origin=panel_origin):
                return JSONResponse({"detail": "foreign_origin"}, status_code=403)
            # NFR-06 (3): when a token is set, mutating requests must carry it
            if api_token and request.headers.get("x-api-token") != api_token:
                return JSONResponse({"detail": "missing_or_bad_token"}, status_code=401)
        return await call_next(request)

    # --- auth check -----------------------------------------------------------
    @app.post("/auth/check")
    def auth_check() -> dict[str, bool]:
        """NFR-06: a side-effect-free authenticated ping. Reaching this handler
        means the security middleware already accepted the x-api-token (or none is
        required) — so a 200 confirms the User Password, a 401 rejects it. The UI
        calls this on Save to tell the operator whether the password is right,
        instead of failing silently on the first real command."""
        return {"ok": True}

    # --- read model -----------------------------------------------------------
    @app.get("/state")
    def get_state() -> dict[str, Any]:
        """UI-05/07/12 dashboard contract: mode, kill/enable state, protection."""
        return {
            "armed": state.armed,
            "stop_trading": state.stop_trading,
            "confirm_live": state.confirm_live,
            "trading_mode": state.trading_mode,
            "entries_enabled": state.entries_enabled(),
            "blocking_state": state.blocking_state(),
        }

    def _snapshot() -> dict[str, Any]:
        return {"state": get_state(), "report": get_report(),
                "entries": get_entries(), "activity": get_activity()}

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        """Read-model delta stream (doc 05 §8). NFR-06: refuse a WS upgrade
        with a foreign Origin. Pushes a snapshot on connect and on each client
        ping; the client renders whatever it receives (no logic client-side)."""
        if not origin_allowed(sock.headers.get("origin"),
                              scheme=sock.url.scheme.replace("ws", "http"),
                              host=sock.headers.get("host", ""),
                              panel_origin=panel_origin):
            await sock.close(code=1008)  # policy violation
            return
        await sock.accept()
        try:
            await sock.send_json(_snapshot())
            while True:
                await sock.receive_text()          # client pings to request a refresh
                await sock.send_json(_snapshot())
        except WebSocketDisconnect:
            return

    @app.get("/report")
    def get_report() -> dict[str, Any]:
        r = day_report(events)
        return {
            "date": r.date, "entries_filled": r.entries_filled, "stops_hit": r.stops_hit,
            "lex_recoveries": r.lex_recoveries, "decay_closes": r.decay_closes,
            "total_credit": str(r.total_credit), "total_fees": str(r.total_fees),
            "day_pnl": str(r.day_pnl), "skips": [list(s) for s in r.skips],
            "per_entry_pnl": {k: str(v) for k, v in r.per_entry_pnl.items()},
        }

    @app.get("/entries")
    def get_entries() -> list[dict[str, Any]]:
        """Per-entry cards (doc 05 §8): one card per armed entry with its
        lifecycle status and running P&L. Pure read model — no logic."""
        day = fold(events)
        cards = []
        for e in day.entries.values():
            cards.append({
                "entry_id": e.entry_id,
                "status": e.status,
                "net_credit": str(e.net_credit),
                "pnl": str(e.pnl),
                "sides_stopped": list(e.sides_stopped),
                "sides_expired": list(e.sides_expired),
                "recovered": e.recoveries != 0,
                "close_initiator": e.close_initiator,
                "placed_at": e.placed_at,               # FEATURE 1: fill time, null if absent
                "legs": _card_legs(e.legs),              # FEATURE 2: per-side strikes/prices
                "premium_received": _premium_received(e.legs) if e.legs else {"PUT": None, "CALL": None},
            })
        cards.sort(key=lambda c: c["entry_id"])
        if entries_enricher is not None:  # FEATURE 3: live P/L, or any future hook
            cards = entries_enricher(cards)
        return cards

    @app.get("/activity")
    def get_activity() -> list[dict[str, str]]:
        """A human-readable feed of the most recent events (newest first),
        so the operator can watch the day unfold. Presentation only."""
        feed = [_describe(ev) for ev in events]
        feed = [f for f in feed if f is not None]
        return list(reversed(feed))[:25]

    # --- UC-02 schedule composition -------------------------------------------
    schedule = ScheduleService(state)

    def _preflight():
        checks = getattr(commands, "preflight_checks", None) or {}
        ok = lambda: (True, "")
        return run_preflight(
            schedule_service=schedule,
            reconcile_clear=checks.get("reconcile", ok),
            clock_ok=checks.get("clock", ok),
            config_ok=checks.get("config", ok),
            market_data_ok=checks.get("market_data", ok),
            # doc 06 s169: max_day_risk is mandatory before live can be enabled.
            require_max_day_risk=(state.trading_mode == "live"),
        )

    @app.get("/schedule")
    def get_schedule() -> dict[str, Any]:
        """The composed rows with their worst-case ESTIMATES, the max_day_risk
        ceiling, and the headroom — so adding a row visibly eats headroom (v1.46)."""
        return schedule.view().to_dict()

    @app.post("/schedule")
    def save_schedule(body: dict[str, Any]) -> dict[str, Any]:
        """UC-02: validate -> version -> persist. Every error, not just the first.
        An invalid schedule is never written: a half-saved one could arm on restart."""
        out = schedule.save(body.get("rows", []), max_day_risk=body.get("max_day_risk"))
        if out["result"] == "invalid":
            raise HTTPException(status_code=422, detail=out)
        return out

    @app.get("/preflight")
    def get_preflight() -> dict[str, Any]:
        """UC-02: the arm checklist, pass/fail per item, in the spec's order."""
        return _preflight().to_dict()

    # --- commands (mutating; secured above) -----------------------------------
    @app.post("/arm")
    def arm() -> dict[str, Any]:
        # ENT-01a: arming requires >= 1 composed, LEGAL entry. The pre-flight runs
        # the whole UC-02 sequence; arm only if every item passed.
        pre = _preflight()
        if not pre.passed:
            raise HTTPException(status_code=400, detail=pre.to_dict())
        state.armed = True
        return {**get_state(), "preflight": pre.to_dict()}

    @app.post("/disarm")
    def disarm() -> dict[str, Any]:
        state.armed = False  # future entries only; positions untouched
        return get_state()

    @app.post("/stop-trading")
    def stop_trading(on: bool = True) -> dict[str, Any]:
        state.stop_trading = on
        return get_state()

    @app.post("/confirm-live")
    def confirm_live(on: bool = True) -> dict[str, Any]:
        state.confirm_live = on
        return get_state()

    @app.post("/config")
    def update_config(patch: dict[str, Any]) -> dict[str, Any]:
        try:
            validate_config(patch)  # UI-03: server-side, reject never clamp
        except (ConfigRejected, StopBasisRejected) as e:
            raise HTTPException(status_code=422, detail={"key": getattr(e, "key", "stop_basis"),
                                                         "reason": e.reason})
        return {"accepted": patch}

    # --- trade actions (UC-14/UI-16) — only when a command surface is wired ----
    if commands is not None:
        @app.get("/entry/{n}/fire-preview")
        def fire_preview(n: int) -> dict[str, Any]:
            """UI-22: what the OK dialog shows — the row's parameters and the
            worst-case ESTIMATE, labelled. No strikes exist yet, so the true number
            cannot be known here; RSK-04 re-prices at fire time and may still veto."""
            rows = schedule.resolved()
            if not 1 <= n <= len(rows):
                raise HTTPException(status_code=404, detail="unknown_entry")
            preview = commands.fire_preview(n, rows[n - 1])
            return {**preview.to_dict(), "can_fire": commands.can_fire()}

        @app.post("/entry/{n}/fire")
        async def fire_entry(n: int, body: dict[str, Any]) -> dict[str, Any]:
            """ENT-09: manual fire. Bypasses ONLY the ENT-02 window; the full
            ENT-03 chain, RSK-08 and RSK-04 run inside the identical pipeline.

            `press_id` makes a double-click exactly one attempt; `confirmed` is the
            simple OK acknowledgement (UI-22 — never a typed phrase), required in
            BOTH paper and live."""
            rows = schedule.resolved()
            if not 1 <= n <= len(rows):
                raise HTTPException(status_code=404, detail="unknown_entry")
            press_id = str(body.get("press_id", "")).strip()
            if not press_id:
                raise HTTPException(status_code=400, detail="press_id_required")
            return await commands.fire(press_id=press_id, entry_number=n,
                                       row=rows[n - 1], confirmed=bool(body.get("confirmed")))

        @app.post("/close/{entry_id}")
        async def close_entry(entry_id: str) -> dict[str, Any]:
            """CLS-02: close one entry instantly via CLS (initiator manual).
            Idempotent — a double-click yields exactly one close."""
            return await commands.close(entry_id)

        @app.post("/flatten")
        async def flatten(body: dict[str, Any]) -> dict[str, Any]:
            """RSK-01a/TC-FLT-01: flatten every open entry — requires a typed
            FLATTEN confirmation (contrast: Close is instant)."""
            result = await commands.flatten(str(body.get("confirmation", "")))
            if result.get("result") == "confirmation_required":
                raise HTTPException(status_code=400, detail="confirmation_required")
            return result

        @app.post("/drill/outage")
        async def outage_drill(body: dict[str, Any] | None = None) -> dict[str, Any]:
            """UC-12: simulate a bot outage and return evidence that the resting
            stops stayed working (with unbroken timestamps) throughout."""
            seconds = float((body or {}).get("outage_seconds", 2.0))
            return await commands.run_outage_drill(seconds)

        @app.post("/mode-switch")
        async def mode_switch(body: dict[str, Any]) -> dict[str, Any]:
            """UC-10/DAY-05: stage a paper/live switch (flat book + typed LIVE for
            live). Effective next day; rejected requests return 400 with a reason."""
            result = await commands.switch_mode(
                str(body.get("target", "")), str(body.get("confirmation", "")))
            if not result["staged"]:
                raise HTTPException(status_code=400, detail=result["reason"])
            return result

    return app
