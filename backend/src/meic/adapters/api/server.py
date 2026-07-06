"""Runnable entrypoint — binds the paper composition to the FastAPI panel.

    uvicorn meic.adapters.api.server:paper_app --factory --host 127.0.0.1 --port 8000

Paper mode only (SIM-01): the live adapter is never constructed here. Seeds a
short demo day into the event log so the read-model endpoints show real
figures on first view.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from meic.composition.paper import PaperComposition
from meic.domain.events import (
    CondorFilled,
    DayArmed,
    EntryClosed,
    LongSold,
    ShortStopped,
    SideExpired,
)
from meic.domain.ticks import TickRung, TickTable

SPX = TickTable((TickRung(Decimal("3.00"), Decimal("0.05")), TickRung(None, Decimal("0.10"))))


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _seed_demo_day(comp: PaperComposition) -> None:
    """A representative paper day so /state and /report aren't empty on load."""
    comp.compose_and_arm(["09:32", "10:00", "10:30", "11:00", "11:30", "12:00"])
    e = comp.events
    e.append(DayArmed(date="2026-07-06", entry_count=6))
    for n in range(1, 7):
        e.append(CondorFilled(entry_id=f"2026-07-06#{n}", net_credit=Decimal("4.00")))
    # entry 2: put stopped (SIM-03 slippage), long LEX-recovered
    e.append(ShortStopped(entry_id="2026-07-06#2", side="PUT", fill=Decimal("3.95"), slippage=Decimal("0.15")))
    e.append(LongSold(entry_id="2026-07-06#2", side="PUT", recovery=Decimal("0.40")))
    # entry 3: decay buyback close
    e.append(ShortStopped(entry_id="2026-07-06#3", side="CALL", fill=Decimal("0.05"), slippage=Decimal("0"), initiator="decay"))
    e.append(EntryClosed(entry_id="2026-07-06#3", initiator="decay"))
    # the rest expire worthless
    for n in (1, 4, 5, 6):
        for side in ("PUT", "CALL"):
            e.append(SideExpired(entry_id=f"2026-07-06#{n}", side=side))


def paper_app():
    comp = PaperComposition(clock=_SystemClock(), ticks=SPX)
    _seed_demo_day(comp)
    from pathlib import Path

    from fastapi.responses import HTMLResponse

    from meic.adapters.api.app import create_app
    app = create_app(comp.state, comp.events)  # no api_token on the localhost demo bind

    # A zero-dependency demo dashboard at "/" so the panel is viewable without a
    # Node/React build. The production UI is the React app in frontend/.
    demo = Path(__file__).resolve().parents[5] / "frontend" / "demo.html"

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return demo.read_text(encoding="utf-8") if demo.exists() else "<h1>MEIC panel</h1>"

    return app
