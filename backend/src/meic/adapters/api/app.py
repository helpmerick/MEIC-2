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

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from meic.application.persistent_state import PersistentState
from meic.config.validation import ConfigRejected, validate_config
from meic.config.stop_basis import StopBasisRejected
from meic.domain.projection import day_report


def create_app(
    state: PersistentState,
    events: list,
    *,
    api_token: str | None = None,
    panel_origin: str = "http://127.0.0.1",
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

    return app
