"""FastAPI control panel — commands + read model (doc 05 §7), NFR-06 secured.

The frontend holds NO trading logic (UI-03): this exposes read-model
projections and command endpoints; all validation is server-side. Security
(NFR-06): localhost-default bind; any MUTATING request (and WS upgrade) with a
foreign Origin is rejected (403); a token, optional on localhost, is required
(and enforced) whenever set. The bot never exposes account-mutating routes
unauthenticated.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from meic.application.persistent_state import PersistentState
from meic.config.validation import ConfigRejected, validate_config
from meic.config.stop_basis import StopBasisRejected
from meic.domain.projection import day_report, fold


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
    }
    if name not in table:
        return None
    icon, label = table[name]
    bits = []
    for attr in ("side", "initiator", "reason", "action"):
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
) -> FastAPI:
    app = FastAPI(title="MEIC control panel")

    @app.middleware("http")
    async def security(request: Request, call_next):
        mutating = request.method in ("POST", "PUT", "DELETE", "PATCH")
        if mutating:
            # NFR-06 (2): reject a foreign Origin even on localhost
            origin = request.headers.get("origin")
            if origin is not None and origin != panel_origin:
                return JSONResponse({"detail": "foreign_origin"}, status_code=403)
            # NFR-06 (3): when a token is set, mutating requests must carry it
            if api_token and request.headers.get("x-api-token") != api_token:
                return JSONResponse({"detail": "missing_or_bad_token"}, status_code=401)
        return await call_next(request)

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
        origin = sock.headers.get("origin")
        if origin is not None and origin != panel_origin:
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
            })
        cards.sort(key=lambda c: c["entry_id"])
        return cards

    @app.get("/activity")
    def get_activity() -> list[dict[str, str]]:
        """A human-readable feed of the most recent events (newest first),
        so the operator can watch the day unfold. Presentation only."""
        feed = [_describe(ev) for ev in events]
        feed = [f for f in feed if f is not None]
        return list(reversed(feed))[:25]

    # --- commands (mutating; secured above) -----------------------------------
    @app.post("/arm")
    def arm() -> dict[str, Any]:
        if not state.entry_schedule:  # ENT-01a: arming an empty schedule is rejected
            raise HTTPException(status_code=400, detail="no_entries_composed")
        state.armed = True
        return get_state()

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

    return app
