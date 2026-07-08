"""Runnable entrypoints behind the FastAPI panel.

Paper (SIM-01) — a self-driving demo day, no credentials, localhost:

    uvicorn meic.adapters.api.server:paper_app --factory --host 127.0.0.1 --port 8000

Live — the real Tastytrade + DXLink wiring, SQLite-persisted (REC-07),
token-gated (NFR-06), booting with SAFE DEFAULTS (DISARMED, Confirm Live OFF)
so nothing trades until the operator deliberately arms and confirms. Defaults
to the CERT sandbox; MEIC_LIVE_IS_TEST=false selects production credentials:

    MEIC_API_TOKEN=... uvicorn meic.adapters.api.server:live_app --factory --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException

from meic.application.clocks import MutableClock
from meic.composition.paper import PaperComposition
from meic.composition.runtime import PaperDemoRuntime
from meic.domain.ticks import TickRung, TickTable

SPX = TickTable((TickRung(Decimal("3.00"), Decimal("0.05")), TickRung(None, Decimal("0.10"))))
ROOT = Path(__file__).resolve().parents[5]

# Wiring PRODUCTION (real money) requires this exact second opt-in alongside
# MEIC_LIVE_IS_TEST=false. One flipped env var must never be enough.
PRODUCTION_OPT_IN = "I_UNDERSTAND_REAL_MONEY"


def _read_env() -> dict[str, str]:
    """Load .env (gitignored, BOM-tolerant per NFR-05), then overlay os.environ."""
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


class _PanelAlerts:
    """AlertSink that keeps critical alerts where the operator can see them
    (RSK-06). A live bot must never swallow a critical alert into /dev/null."""

    def __init__(self, cap: int = 100) -> None:
        self._alerts: list[dict] = []
        self._cap = cap

    def alert(self, level: str, message: str, **context) -> None:
        self._alerts.append({"level": level, "message": message,
                             "context": {k: str(v) for k, v in context.items()}})
        del self._alerts[: -self._cap]

    def recent(self) -> list[dict]:
        return list(reversed(self._alerts))


def _serve_panel(app) -> None:
    """Mount the built React panel at / (falls back to demo.html, then a stub)."""
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles

    dist = ROOT / "frontend" / "dist"
    demo = ROOT / "frontend" / "demo.html"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        if (dist / "index.html").exists():
            return FileResponse(str(dist / "index.html"))
        return HTMLResponse(demo.read_text(encoding="utf-8") if demo.exists() else "<h1>MEIC</h1>")

    @app.get("/demo", response_class=HTMLResponse)
    def demo_page() -> str:
        return demo.read_text(encoding="utf-8") if demo.exists() else "<h1>MEIC</h1>"


def paper_app():
    """Paper-mode demo: a compressed day loops so the panel shows activity."""
    from meic.adapters.api.app import create_app
    from meic.composition.panel_commands import PanelCommands

    comp = PaperComposition(clock=MutableClock(datetime(2026, 7, 7, 9, 30, tzinfo=timezone.utc)), ticks=SPX)
    runtime = PaperDemoRuntime(comp, step_seconds=3.0)

    # no api_token on the localhost demo bind; Close/Flatten act on the live book
    app = create_app(comp.state, comp.events, commands=PanelCommands(comp))

    @app.on_event("startup")
    async def _start() -> None:
        app.state.runtime = asyncio.create_task(runtime.run_forever())

    @app.on_event("shutdown")
    async def _stop() -> None:
        task = getattr(app.state, "runtime", None)
        if task:
            task.cancel()

    _serve_panel(app)
    return app


def live_app():
    """Live composition behind the panel: real broker + feed, SQLite-persisted,
    token-gated, safe defaults. Connects on startup; NO trading auto-starts —
    the operator arms + confirms live deliberately. CERT sandbox by default."""
    from meic.adapters.api.app import create_app
    from meic.adapters.persistence.event_store import SqliteStateStore
    from meic.application.clocks import SystemClock
    from meic.composition.live import LiveComposition
    from meic.composition.panel_commands import PanelCommands

    env = _read_env()
    is_test = env.get("MEIC_LIVE_IS_TEST", "true").lower() != "false"
    token = env.get("MEIC_API_TOKEN")
    if not token:
        raise RuntimeError("live panel requires MEIC_API_TOKEN (NFR-06) — set it in .env/env")

    # Real money needs a SECOND, deliberate opt-in: flipping one env var must not
    # be enough. The adapter separately asserts the token's issuer is production.
    if not is_test and env.get("MEIC_ALLOW_PRODUCTION") != PRODUCTION_OPT_IN:
        raise RuntimeError(
            "REFUSING to wire PRODUCTION (real money): set "
            f"MEIC_ALLOW_PRODUCTION={PRODUCTION_OPT_IN} to confirm, in addition to "
            "MEIC_LIVE_IS_TEST=false. Two deliberate switches, never one.")

    kind = "CERT" if is_test else "PROD"
    secret = env.get(f"TT_{kind}_PROVIDER_SECRET")
    refresh = env.get(f"TT_{kind}_REFRESH_TOKEN")
    account = env.get(f"TT_{kind}_ACCOUNT")
    if not (secret and refresh):
        raise RuntimeError(f"missing {kind} broker credentials (TT_{kind}_PROVIDER_SECRET / _REFRESH_TOKEN)")

    data_dir = Path(env.get("MEIC_DATA_DIR", str(ROOT / "data")))
    data_dir.mkdir(parents=True, exist_ok=True)

    comp = LiveComposition(
        clock=SystemClock(), ticks=SPX, provider_secret=secret, refresh_token=refresh,
        is_test=is_test, state_store=SqliteStateStore(data_dir / "state.db"))
    app = create_app(comp.state, comp.events, api_token=token, commands=PanelCommands(comp))
    app.state.composition = comp
    app.state.broker_connected = False
    app.state.broker_error = None
    app.state.reconcile = None
    alerts = _PanelAlerts()
    app.state.alerts = alerts
    comp.alerts = alerts  # critical alerts must reach the operator, not /dev/null

    async def _boot_reconcile() -> None:
        """REC-02/04: adopt broker truth before any trading is possible. Anything
        the bot's durable ledger cannot account for is FOREIGN -> quarantined and
        entries stay blocked until the operator resolves it."""
        from meic.application.reconcile_boot import reconcile_on_boot

        result = await reconcile_on_boot(
            broker=comp.broker, events=comp.events, state=comp.state, alerts=alerts)
        app.state.reconcile = result

    @app.on_event("startup")
    async def _connect() -> None:
        # A broker/network hiccup must NOT take down the operator's control
        # panel: come up regardless, record the status, let it be retried.
        try:
            await comp.connect(account)
            app.state.broker_connected = True
            await _boot_reconcile()
        except Exception as exc:  # noqa: BLE001 — surfaced, never fatal
            app.state.broker_error = repr(exc)

    @app.post("/broker/connect")
    async def broker_connect() -> dict:
        """Retry the broker session + boot reconcile (token-gated by middleware)."""
        try:
            await comp.connect(account)
            app.state.broker_connected = True
            app.state.broker_error = None
            await _boot_reconcile()
        except Exception as exc:  # noqa: BLE001
            app.state.broker_connected = False
            app.state.broker_error = repr(exc)
        return {"connected": app.state.broker_connected, "error": app.state.broker_error}

    @app.get("/broker/health")
    def broker_health() -> dict:
        from meic.application.reconcile_boot import entries_blocked_by_reconcile
        return {"connected": app.state.broker_connected, "error": app.state.broker_error,
                "entries_blocked_by_reconcile": entries_blocked_by_reconcile(comp.events)}

    @app.get("/reconcile")
    def reconcile_status() -> dict:
        r = app.state.reconcile
        if r is None:
            return {"ran": False}
        return {"ran": True, "adopted": r.adopted, "foreign": r.foreign,
                "shortfall": r.shortfall, "stops_placed": [list(s) for s in r.stops_placed],
                "lex_resumed": [list(s) for s in r.lex_resumed],
                "mismatches": r.mismatches, "entries_blocked": r.entries_blocked}

    @app.get("/alerts")
    def recent_alerts() -> list[dict]:
        return alerts.recent()

    # --- live trading day: selection + gates + the wall-clock runtime ---------
    from meic.composition.live_gates import ET, LiveMarketGates
    from meic.composition.live_runtime import LiveRuntime
    from meic.composition.live_selection import LiveCondorSelector, SelectionConfig

    min_buying_power = Decimal(env.get("MEIC_MIN_BUYING_POWER", "5000"))

    class _Snapshots:
        """Holds the freshness of the most recent chain snapshot so the DAT-02
        gate reflects the data the selector actually used."""
        stale = True

        async def take(self):
            from meic.adapters.dxlink.chain_snapshot import snapshot_chain
            snap = await snapshot_chain(comp.broker._session)
            self.stale = snap.stale
            return snap

    snaps = _Snapshots()

    async def _data_fresh() -> bool:
        return not snaps.stale

    async def _session_valid() -> bool:
        await comp.broker.working_orders()  # a light authenticated call; raises if dead
        return True

    async def _buying_power_ok() -> bool:
        return (await comp.broker.buying_power()) >= min_buying_power

    runtime = LiveRuntime(
        comp,
        selector=LiveCondorSelector(snapshot_provider=snaps.take, config=SelectionConfig()),
        market_gates=LiveMarketGates(clock=comp.clock, data_fresh=_data_fresh,
                                     session_valid=_session_valid,
                                     buying_power_ok=_buying_power_ok),
    )
    app.state.runtime = runtime
    app.state.day_task = None

    def _todays_entry_times() -> list[datetime]:
        """Composed 'HH:MM' schedule -> today's ET datetimes."""
        today = datetime.now(ET).date()
        out = []
        for item in comp.state.entry_schedule:
            hh, mm = str(item["time"]).split(":")[:2]
            out.append(datetime.combine(today, dtime(int(hh), int(mm)), tzinfo=ET))
        return sorted(out)

    @app.post("/day/start")
    async def day_start() -> dict:
        """Start the wall-clock trading day. Every entry still runs the full gate
        chain — starting the day does NOT arm it."""
        task = app.state.day_task
        if task is not None and not task.done():
            return {"running": True, "already_running": True}
        times = _todays_entry_times()
        if not times:
            raise HTTPException(status_code=400, detail="no_entries_composed")
        day = datetime.now(ET).date().isoformat()
        app.state.day_task = asyncio.create_task(runtime.run_day(day, times))
        return {"running": True, "day": day, "entries": len(times)}

    @app.post("/day/stop")
    async def day_stop() -> dict:
        task = app.state.day_task
        if task is not None and not task.done():
            task.cancel()
        return {"running": False}

    @app.get("/day/status")
    def day_status() -> dict:
        task = app.state.day_task
        if task is None:
            return {"started": False, "running": False}
        if not task.done():
            return {"started": True, "running": True}
        if task.cancelled():
            return {"started": True, "running": False, "cancelled": True}
        exc = task.exception()
        return {"started": True, "running": False,
                "filled": None if exc else task.result(),
                "error": repr(exc) if exc else None}

    _serve_panel(app)
    return app
