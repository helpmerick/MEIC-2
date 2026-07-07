"""Runnable entrypoint — a LIVE paper runtime behind the FastAPI panel.

    uvicorn meic.adapters.api.server:paper_app --factory --host 127.0.0.1 --port 8000

Paper mode only (SIM-01). On startup it launches PaperDemoRuntime, which drives
a compressed paper day (arms, fires entries, protects, stops one side out,
LEX-recovers, decays one, settles EOD) on a loop — so the read-model endpoints
and the panel show a day unfold in real time. The React app is served at "/".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from meic.application.clocks import MutableClock
from meic.composition.paper import PaperComposition
from meic.composition.runtime import PaperDemoRuntime
from meic.domain.ticks import TickRung, TickTable

SPX = TickTable((TickRung(Decimal("3.00"), Decimal("0.05")), TickRung(None, Decimal("0.10"))))


def paper_app():
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles

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

    root = Path(__file__).resolve().parents[5]
    dist = root / "frontend" / "dist"
    demo = root / "frontend" / "demo.html"
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

    return app
