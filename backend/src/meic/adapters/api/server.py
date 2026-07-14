"""Runnable entrypoints behind the FastAPI panel.

Paper (SIM-01) — a self-driving demo day, no credentials, localhost:

    uvicorn meic.adapters.api.server:paper_app --factory --host 127.0.0.1 --port 8010

Live — the real Tastytrade + DXLink wiring, SQLite-persisted (REC-07),
token-gated (NFR-06), booting with SAFE DEFAULTS (DISARMED, Confirm Live OFF)
so nothing trades until the operator deliberately arms and confirms. Defaults
to the CERT sandbox; MEIC_LIVE_IS_TEST=false selects production credentials:

    MEIC_USER_PASSWORD=... uvicorn meic.adapters.api.server:live_app --factory --host 127.0.0.1 --port 8010
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException, Query

from meic.adapters.api.app import _strike_from_symbol
from meic.application.clocks import MutableClock
from meic.application.market_calendar import (
    is_trading_day,
    next_trading_day,
    trading_day,
    trading_day_str,
)
from meic.application.nyse_holidays import half_days_near, holidays_near, nyse_holidays
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
            # no-cache (2026-07-11): without it browsers heuristically cache
            # index.html and keep serving a STALE panel after a deploy — the
            # operator repeatedly saw old UI until a hard refresh. no-cache
            # forces revalidation each load (cheap: ETag/304); the hashed
            # /assets bundles it references stay immutable-cacheable.
            return FileResponse(str(dist / "index.html"),
                                headers={"Cache-Control": "no-cache"})
        return HTMLResponse(demo.read_text(encoding="utf-8") if demo.exists() else "<h1>MEIC</h1>")

    @app.get("/demo", response_class=HTMLResponse)
    def demo_page() -> str:
        return demo.read_text(encoding="utf-8") if demo.exists() else "<h1>MEIC</h1>"


def paper_app():
    """Paper-mode demo: a compressed day loops so the panel shows activity."""
    from decimal import Decimal

    from meic.adapters.api.app import create_app
    from meic.application.entry_gates import GateSnapshot, RiskSnapshot
    from meic.application.manual_entry import ManualEntry
    from meic.composition.panel_commands import PanelCommands
    from meic.domain.projection import fold

    comp = PaperComposition(clock=MutableClock(datetime(2026, 7, 7, 9, 30, tzinfo=timezone.utc)), ticks=SPX)
    comp.state.trading_mode = "paper"   # honest: this process holds the simulator
    runtime = PaperDemoRuntime(comp, step_seconds=3.0)

    async def selector(when, n, config=None, put_floor=None, call_floor=None):
        """The demo's stand-in for live selection. A real day selects from the
        DXLink chain; the shape — and the row's contracts — is identical.
        `put_floor`/`call_floor` (ENT-09b v1.57): accepted for interface
        parity with the real selector (ManualEntry.fire always passes them),
        but the demo's synthetic condor has no real chain to filter."""
        return runtime._condor(n, config.contracts if config else 1), None

    async def gates():
        """ENT-03 market/session portion. The durable states (ARMED / Stop Trading /
        Confirm Live) come from PersistentState and are NOT overridden here."""
        return GateSnapshot(
            armed=comp.state.armed, confirm_live=comp.state.confirm_live,
            stop_trading=comp.state.stop_trading, flatten_in_progress=False,
            market_open=True, market_halted=False, data_fresh=True,
            session_valid=True, buying_power_ok=True)

    def risk():
        """RSK-04 with REAL inputs: only entries still open count, and the ceiling
        is whatever the operator typed into the schedule panel."""
        open_ids = {eid for eid, e in fold(comp.events).entries.items() if not e.close_initiator}
        ceiling = comp.state.max_day_risk
        return RiskSnapshot(
            new_worst_case=Decimal("0"),   # attempt() re-prices it from the condor
            open_worst_cases=tuple(wc for eid, wc in comp.worst_case.items() if eid in open_ids),
            max_day_risk=None if ceiling in (None, "") else Decimal(str(ceiling)),
            buying_power=comp.broker.ledger.buying_power)   # SIM-04
    manual = ManualEntry(comp, selector, gates, max_entries_per_day=6,
                         risk=risk, day=lambda: "2026-07-07")

    # no api_token on the localhost demo bind; Close/Flatten act on the live book
    app = create_app(comp.state, comp.events,
                     commands=PanelCommands(comp, manual_entry=manual,
                                            default_drill_outage_seconds=_drill_outage_seconds(_read_env())),
                     reporting_config=_reporting_config(_read_env()))

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


def _chain_completeness_pct(env: dict[str, str]) -> Decimal:
    """STK-10 `chain_completeness_pct` (doc 06: range 50-100, default 90) — the % of
    the entry's TRADE-RELATIVE reachable strike set (v1.51: probe range + wings +
    STK-09 shift budgets — never a fixed ATM band, retired) that must carry marks
    before selection. Out-of-range falls back to the spec default (reject-the-dial,
    trade the default)."""
    try:
        raw = Decimal(env.get("MEIC_CHAIN_COMPLETENESS_PCT", "90"))
    except (ArithmeticError, ValueError):
        return Decimal("90")
    return raw if Decimal("50") <= raw <= Decimal("100") else Decimal("90")


def _drill_outage_seconds(env: dict[str, str]) -> float:
    """UC-12 `drill_outage_seconds` (doc 06: range 10-300, default 60) -- the
    stop-independence drill's default disconnect duration when a request
    doesn't specify its own. Out-of-range falls back to the spec default (the
    same reject-the-dial convention as `_chain_completeness_pct` above)."""
    try:
        raw = float(env.get("MEIC_DRILL_OUTAGE_SECONDS", "60"))
    except ValueError:
        return 60.0
    return raw if 10 <= raw <= 300 else 60.0


def _min_validated_strikes(env: dict[str, str]) -> int:
    """STK-10 v1.55 `min_validated_strikes` (doc 06: range 3-40, default 10) --
    the per-side viability floor on the baseline-captured validated universe
    (domain/chain.py: `validated_universe`). Out-of-range falls back to the
    spec default (the same reject-the-dial convention as
    `_chain_completeness_pct` above)."""
    try:
        raw = int(env.get("MEIC_MIN_VALIDATED_STRIKES", "10"))
    except ValueError:
        return 10
    return raw if 3 <= raw <= 40 else 10


def _entry_window_seconds(env: dict[str, str]) -> int:
    """STK-10 v1.51 / ENT-02 (doc 06: range 10-600, default 120) — how long the
    selector's own retry loop may keep taking fresh snapshots (every
    `chain_retry_seconds`) after `when` before giving up with `incomplete_chain`
    (or the walk's own reason). Out-of-range falls back to the spec default."""
    try:
        raw = int(env.get("MEIC_ENTRY_WINDOW_SECONDS", "120"))
    except ValueError:
        return 120
    return raw if 10 <= raw <= 600 else 120


def _chain_retry_seconds(env: dict[str, str]) -> int:
    """STK-10 `chain_retry_seconds` (doc 06: range 1-30, default 5) — the interval
    between fresh-snapshot retries while the reachable-set gate is unhealed or a
    wing is missing, bounded by the entry window above. Out-of-range falls back
    to the spec default."""
    try:
        raw = int(env.get("MEIC_CHAIN_RETRY_SECONDS", "5"))
    except ValueError:
        return 5
    return raw if 1 <= raw <= 30 else 5


def _warmup_lead_seconds(env: dict[str, str]) -> float:
    """ENT-08 `session_warmup_lead_seconds` (doc 06: range 10-300, default 60)
    — how far ahead of each scheduled entry the real warm-up runs. Out-of-range
    falls back to the spec default (the same reject-the-dial convention as
    `_chain_completeness_pct` above)."""
    try:
        raw = float(env.get("MEIC_SESSION_WARMUP_LEAD_SECONDS", "60"))
    except ValueError:
        return 60.0
    return raw if 10 <= raw <= 300 else 60.0


def _stop_fill_poll_seconds(env: dict[str, str]) -> float:
    """ITEM 1 (operator ruling 2026-07-11) fallback-poll interval: range
    5-120, default 15 -- how often the dedicated stop-fill poll loop
    re-runs `detect_and_recover_stop_fills` (skip-if-busy against
    `stop_fill_lock`, see order_event_watch.run_pass_if_idle) as a fallback
    for whatever the order-event push consumer hasn't already caught. An
    infra polling dial, same class as `MEIC_HEALTH_INTERVAL_S`, not a
    doc-06 strategy config. Out-of-range falls back to the default (the
    same reject-the-dial convention as `_warmup_lead_seconds` above)."""
    try:
        raw = float(env.get("MEIC_STOP_FILL_POLL_S", "15"))
    except ValueError:
        return 15.0
    return raw if 5 <= raw <= 120 else 15.0


def _max_quote_age_ms(env: dict[str, str]) -> int:
    """DAT-02 `max_quote_age_ms` (doc 06: range 500-15000, default 3000) — NFR-04
    (2026-07-13): the freshness bar a QuoteHub mark must clear to be used LIVE
    (`_resolve_leg_mid`); a mark older than this is treated as ABSENT and falls
    through to the existing chain-snapshot path, never used stale. Out-of-range
    falls back to the spec default (the same reject-the-dial convention as
    `_chain_completeness_pct` above)."""
    try:
        raw = int(env.get("MEIC_MAX_QUOTE_AGE_MS", "3000"))
    except ValueError:
        return 3000
    return raw if 500 <= raw <= 15000 else 3000


def _quote_stream_poll_seconds(env: dict[str, str]) -> float:
    """NFR-04 (2026-07-13) quote-stream loop cadence: range 1-60, default 5 --
    how long the loop idles between checks when there are no open entries to
    subscribe to, and how long it backs off after a stream failure before
    retrying. An infra polling dial, same class as `MEIC_HEALTH_INTERVAL_S` /
    `MEIC_STOP_FILL_POLL_S` above, not a doc-06 strategy config. Out-of-range
    falls back to the default (the same reject-the-dial convention as
    `_stop_fill_poll_seconds` above)."""
    try:
        raw = float(env.get("MEIC_QUOTE_STREAM_POLL_S", "5"))
    except ValueError:
        return 5.0
    return raw if 1 <= raw <= 60 else 5.0


def _watchdog_grace_seconds(env: dict[str, str]) -> Decimal:
    """STP-03b `watchdog_grace_seconds` (doc 06: range 3-60, default 10) — how
    long a short's mark may sit at/above its trigger with the resting stop
    unfilled before the STP-03b watchdog raises its critical alert. Out-of-
    range falls back to the spec default (the same reject-the-dial convention
    as `_chain_completeness_pct` above)."""
    try:
        raw = Decimal(env.get("MEIC_WATCHDOG_GRACE_SECONDS", "10"))
    except (ArithmeticError, ValueError):
        return Decimal("10")
    return raw if Decimal("3") <= raw <= Decimal("60") else Decimal("10")


def _watchdog_escalate_seconds(env: dict[str, str]) -> Decimal:
    """STP-03b `watchdog_escalate_seconds` (doc 06: range 5-120, default 20) —
    total elapsed time from the FIRST breach at which the watchdog fires its
    own marketable buy-to-close and cancels the sleeping stop. Out-of-range
    falls back to the spec default (the same reject-the-dial convention as
    `_chain_completeness_pct` above)."""
    try:
        raw = Decimal(env.get("MEIC_WATCHDOG_ESCALATE_SECONDS", "20"))
    except (ArithmeticError, ValueError):
        return Decimal("20")
    return raw if Decimal("5") <= raw <= Decimal("120") else Decimal("20")


def _settlement_lookback_days(env: dict[str, str]) -> int:
    """EOD-01 v1.59 follow-up (2026-07-13): how many RECENT prior trading days
    `_maybe_eod_reconcile_once`'s look-back re-checks for a settlement that
    posted LATE (the root cause this dial fixes -- an SPX 0DTE settlement
    posts to the broker's Receive-Deliver ledger the day AFTER the trading
    day, so the ordinary same-day 16:15 capture can legitimately find nothing
    yet; without a look-back that settlement is NEVER captured again once the
    day's `already` gate seals it). Infra polling dial, same class as
    `MEIC_QUOTE_STREAM_POLL_S` / `MEIC_WATCHDOG_GRACE_SECONDS` above -- range
    1-30, default 5. Out-of-range falls back to the default (the same
    reject-the-dial convention as `_chain_completeness_pct` above)."""
    try:
        raw = int(env.get("MEIC_SETTLEMENT_LOOKBACK_DAYS", "5"))
    except ValueError:
        return 5
    return raw if 1 <= raw <= 30 else 5


def _reporting_capital_base(env: dict[str, str]) -> Decimal | None:
    """RPT-04/doc 06 `reporting_capital_base` ($ > 0, no spec default --
    "required for return metrics"). Operator-set only: account net-liq is
    REJECTED (D1 -- foreign capital would pollute ROC). Absent, unparsable,
    or <= 0 -> None, which reports.py renders as "unconfigured" rather than
    inventing a denominator."""
    raw = env.get("MEIC_REPORTING_CAPITAL_BASE")
    if not raw:
        return None
    try:
        base = Decimal(raw)
    except (ArithmeticError, ValueError):
        return None
    return base if base > 0 else None


def _sharpe_risk_free_pct(env: dict[str, str]) -> Decimal:
    """RPT-04 `sharpe_risk_free_pct` (doc 06: range 0-10 step 0.25, default 0,
    D3). Out-of-range or off-step falls back to the spec default (the same
    reject-the-dial convention as `_chain_completeness_pct` above)."""
    try:
        raw = Decimal(env.get("MEIC_SHARPE_RISK_FREE_PCT", "0"))
    except (ArithmeticError, ValueError):
        return Decimal("0")
    if not (Decimal("0") <= raw <= Decimal("10")):
        return Decimal("0")
    if (raw * 4) % 1 != 0:  # must land on a 0.25 step
        return Decimal("0")
    return raw


def _report_min_sample_days(env: dict[str, str]) -> int:
    """RPT-04 `report_min_sample_days` (doc 06: range 5-100, default 20, D2)."""
    try:
        raw = int(env.get("MEIC_REPORT_MIN_SAMPLE_DAYS", "20"))
    except ValueError:
        return 20
    return raw if 5 <= raw <= 100 else 20


def _reporting_config(env: dict[str, str], *, stop_loss_pct=None):
    from meic.adapters.api.reports import ReportingConfig

    return ReportingConfig(
        capital_base=_reporting_capital_base(env),
        rf_pct=_sharpe_risk_free_pct(env),
        min_sample_days=_report_min_sample_days(env),
        stop_loss_pct=stop_loss_pct)


def _current_stop_loss_pct(state):
    """RPT-03 contract audit's reference pct. KNOWN LIMITATION (slice-2
    handoff item): the event log does not yet carry each FILLED entry's OWN
    stop_loss_pct (CondorFilled/StopPlaced record the trigger PRICE, never
    the pct that produced it), so this is a best-effort proxy -- the
    CURRENTLY CONFIGURED schedule's first row, re-read live on every request
    (never baked into a stale config snapshot) -- falling back to the domain
    schedule default (95%, `domain/schedule.py`) when no row is configured.
    A future slice should record the pct on CondorFilled/StopPlaced directly
    and retire this proxy."""
    from decimal import Decimal as _D

    rows = state.entry_schedule or []
    if rows and isinstance(rows[0], dict) and rows[0].get("stop_loss_pct") is not None:
        try:
            return _D(str(rows[0]["stop_loss_pct"])) / 100
        except (ArithmeticError, ValueError):
            pass
    return _D("0.95")


def _remaining_rows(rows, now, events, day):
    """ENT-10: the rows a day task started NOW should attempt — future-timed
    (row.when > now) and not already attempted today (no CondorFilled with
    entry_id == f"{day}#{n}" and no EntrySkipped with date==day and
    entry_number==n in events).

    `row.number` is the row's DURABLE entry id (ENT-10(4), v1.53, operator
    ruling) — assigned once at Save and carried through by `schedule_rows` —
    NOT its position in `rows`. Positions are irrelevant here: a mid-day
    delete/re-save while ARMED can add, drop or reorder rows, and filtering
    must never renumber a survivor or double-assign an id. The `idx` fallback
    below only applies to a bare row with no stamped number at all (the
    offline scheduler, or a pre-v1.53 persisted schedule that predates ids).
    """
    from meic.domain.events import CondorFilled, EntrySkipped

    filled_ids = {e.entry_id for e in events if isinstance(e, CondorFilled)}
    skipped = {(e.date, e.entry_number) for e in events if isinstance(e, EntrySkipped)}

    out = []
    for idx, row in enumerate(rows, start=1):
        n = row.number if row.number is not None else idx
        if row.when <= now:
            continue
        if f"{day}#{n}" in filled_ids:
            continue
        if (day, n) in skipped:
            continue
        out.append(row)
    return out


def _day_status_extras(rows, now):
    """UI-24: (next_entry_at_iso|None, seconds_to_next|None, entries_remaining)
    computed from row.when > now."""
    remaining = [r for r in rows if r.when > now]
    if not remaining:
        return {"next_entry_at": None, "seconds_to_next": None, "entries_remaining": 0}
    nxt = min(remaining, key=lambda r: r.when)
    return {
        "next_entry_at": nxt.when.isoformat(),
        # UI-24 (v1.62): the difference of REAL INSTANTS (epoch), never
        # wall-clock arithmetic. Same-tzinfo aware subtraction is defined by
        # the stdlib as the NAIVE wall-clock difference, which drops the DST
        # fall-back hour on a span crossing the switch (one hour short —
        # 172560s instead of the true 176160s in TC-DAY-07 scenario 5, which
        # pins the corrected value). `.timestamp()` compares the UTC instants.
        "seconds_to_next": int(nxt.when.timestamp() - now.timestamp()),
        "entries_remaining": len(remaining),
    }


def _next_trading_day_extras(state, now):
    """DAY-01/UI-24 (operator ruling 2026-07-11): on a NON-trading day the
    watch strip must not promise an entry today — a Saturday used to show
    "next entry 11:56 ET — in 7:03:05". Roll the countdown to the next trading
    day's first entry instead: same shape as `_day_status_extras`, with
    `seconds_to_next` spanning the closed days.

    Only ever called on weekends/holidays. On a trading day an exhausted
    schedule still reads "no more entries today" — TC-UI-06 locks that wording,
    and the standing schedule genuinely fires nothing more until midnight ET.
    """
    from meic.composition.live_gates import ET
    from meic.composition.live_wiring import schedule_rows

    today = trading_day(now)  # DAY-03: ET, not `now`'s own (possibly UTC) `.date()`
    day = next_trading_day(today, holidays=holidays_near(today))
    return _day_status_extras(schedule_rows(state, today=day, tz=ET), now)


async def _supervise_once(app_state, comp, alerts, todays_rows, runtime, now_fn) -> None:
    """ENT-10: one supervisor tick, factored out of `live_app`'s startup loop so it
    can be unit-tested without a running FastAPI app. Precedence, evaluated in
    order:

      1. Disarmed -> clear the crash latch (ENT-10(6)) and cancel any running
         task (ENT-10(3)).
      2. A task is already running -> leave it alone.
      3. The crash latch is set -> do NOT auto-restart (ENT-10(6)) until the
         operator cycles Disarm -> Arm.
      4. The previous task finished WITH an exception and is not yet latched ->
         latch it, raise a critical alert (RSK-06), and do NOT start a new task
         on this same pass (a crash must be an alert, never a retry loop).
      5. Otherwise (no task yet, or the previous task finished OK / was
         cancelled) -> start a new task for the remaining, originally-numbered
         rows, if any remain.
    """
    armed = comp.state.armed
    task = app_state.day_task
    running = task is not None and not task.done()

    if not armed:
        app_state.day_task_failed = False   # a disarm clears the crash latch (ENT-10(6))
        if running:
            task.cancel()                   # ENT-10(3)
        # drop the stale reference so a later re-arm doesn't re-detect this task's
        # old exception (ENT-10(6): the disarm→arm cycle must actually restart the day)
        app_state.day_task = None
        return

    if running:
        return

    if app_state.day_task_failed:
        return                              # ENT-10(6): no auto-restart after a crash

    if task is not None and task.done() and not task.cancelled() and task.exception() is not None:
        app_state.day_task_failed = True
        alerts.alert("critical", "ENT-10: day task died; disarm+arm to restart",
                     error=repr(task.exception()))
        return

    now = now_fn()

    # DAY-01 (operator ruling 2026-07-11): consult the exchange calendar BEFORE
    # scheduling entries. A weekend or market holiday gets no day task at all —
    # previously every closed day started one whose entries were then each
    # refused by the at-fire-time ENT-03 market-open gate (which remains, as
    # the safety net), writing EntrySkipped noise into the event log.
    #
    # DAY-03: `today` is the ET trading day (`trading_day`, the one shared
    # helper) — previously computed here via an ad hoc ET conversion and then
    # DISCARDED: the two lines below used to re-derive `now.date().isoformat()`
    # directly, which is `now`'s own (UTC) date whenever `now_fn` is a real
    # UTC clock, silently contradicting the trading-day check just above.
    today = trading_day(now)
    if not is_trading_day(today, holidays=holidays_near(today)):
        return

    day = today.isoformat()
    rows = _remaining_rows(todays_rows(), now, comp.events, day)
    if rows:
        app_state.day_task = asyncio.create_task(runtime.run_day(day, rows))


async def _supervisor_tick(app_state, comp, alerts, todays_rows, runtime, now_fn) -> None:
    """One GUARDED supervisor tick. A broken tick must be VISIBLE (RSK-06): a bug
    in the schedule read would otherwise silently prevent the day from ever
    starting. Alert once per DISTINCT error — not every interval — by latching the
    last failure's repr on `app_state.day_supervisor_error` (None when healthy;
    surfaced in /day/status as `supervisor_error`)."""
    try:
        await _supervise_once(app_state, comp, alerts, todays_rows, runtime, now_fn)
        app_state.day_supervisor_error = None   # a clean tick clears the latch
    except Exception as exc:  # noqa: BLE001
        err = repr(exc)
        if err != app_state.day_supervisor_error:
            app_state.day_supervisor_error = err
            alerts.alert("critical", f"ENT-10: day supervisor tick failed: {err}")


class _BrokerReadFacade:
    """RPT-15: the ONLY thing `ReportReconciler` ever sees of the broker --
    plain read-only forwards. Deliberately declared here (adapters/api), not
    in application/report_reconciler.py, which imports NOTHING from
    meic.adapters at all (tests/application/test_report_reconciler_structural.py
    asserts this): this wrapper is what makes that true, by holding the ONLY
    reference to the real `TastytradeAdapter` (`comp.broker`) and exposing
    NOTHING beyond these methods -- no submit/replace/cancel is even
    reachable through it. `day_settlements` (RPT-16, operator ruling
    2026-07-10) is the same shape -- application/backfill.py never sees
    `comp.broker` directly either.
    """

    def __init__(self, broker) -> None:
        self._broker = broker

    async def positions(self):
        return await self._broker.positions()

    async def day_fills(self, day: str):
        return await self._broker.day_fills(day)

    async def day_settlements(self, day: str):
        return await self._broker.day_settlements(day)

    async def cash_and_fees(self, day: str):
        return await self._broker.cash_and_fees(day)


EOD_RECONCILE_TIME = dtime(16, 15)  # RPT-15: after EOD-01 settlement each trading day


def _has_settlement_pending(events, day: str) -> bool:
    """Cheap, log-only "does `day` still need a settlement capture?" check --
    NO broker call. Reuses `domain.projection.fold`'s existing
    `EntryProjection.settlement_pending` (never a new notion of pending-ness):
    True iff at least one of `day`'s own entries (by the `"{day}#{n}"` id
    prefix, `reporting.folds.entry_day`'s convention) still has an unresolved
    short leg with no `SettlementRecorded` captured for its symbol."""
    from meic.domain.projection import fold
    from meic.reporting.folds import entry_day

    state = fold(events)
    return any(entry_day(entry_id) == day and entry.settlement_pending
              for entry_id, entry in state.entries.items())


def _mark_expired_sides(events, day: str) -> None:
    """EOD-01 v1.59: "After settlement, the bot marks all remaining sides
    EXPIRED." Runs AFTER settlement capture for `day` (both the same-day
    path and the look-back path in `_maybe_eod_reconcile_once` below) --
    log-only, no broker call, so it is cheap and safe to attempt every tick.

    For each of `day`'s own entries, a side is marked `SideExpired` iff ALL
    of:
      - REMAINING: not in `sides_stopped`, not in `sides_closed`, and the
        entry has no `close_initiator` at all -- a stopped/LEX'd/decay
        -closed/operator-closed side never expires; the OTHER (surviving)
        side of the SAME entry still can.
      - SETTLED: the side's SHORT leg symbol is already in
        `EntryProjection.settled_symbols` -- i.e. the broker has actually
        journaled a `SettlementRecorded` for it. This is the per-side
        inverse of `EntryProjection.settlement_pending` (domain/projection.py):
        the SAME broker-truth predicate, never a new notion of expiry, never
        a guess from a clock or computed moneyness. Marks a side regardless
        of whether it finished OTM or ITM -- the cash effect either way is
        already carried in `settlements`; EOD-01 marks ALL remaining sides
        EXPIRED, not just the worthless ones.
      - NOT ALREADY MARKED: idempotent, never appends a second `SideExpired`
        for the same (entry_id, side).
    """
    from meic.domain.events import SideExpired
    from meic.domain.projection import fold
    from meic.reporting.folds import entry_day

    state = fold(events)
    for entry_id, entry in state.entries.items():
        if entry_day(entry_id) != day:
            continue
        if entry.close_initiator is not None:
            continue
        for leg in entry.legs:
            if leg.role != "short":
                continue
            side = leg.side
            if side in entry.sides_stopped or side in entry.sides_closed:
                continue
            if side in entry.sides_expired:
                continue
            if leg.symbol not in entry.settled_symbols:
                continue
            events.append(SideExpired(entry_id=entry_id, side=side))


async def _maybe_eod_reconcile_once(app_state, comp, reconciler, now_fn, broker_reads=None,
                                    *, lookback_days: int = 5) -> None:
    """RPT-15: after `EOD_RECONCILE_TIME` ET on a trading day (RPT-01: any ET
    day with >= 1 entry attempt), run the reconciler ONCE for that day.
    Idempotent by construction: a day already carrying a `DayBrokerConfirmed`
    OR a `CorrectionRecord` with `scope == "own"` has already been resolved
    (matched or corrected) and is skipped; a day with neither (never
    reconciled, or the broker was unreachable last time) is retried --
    exactly RPT-15's "stays bot-computed... retries at next boot/reconcile"
    rule. Factored out of `live_app`'s health loop, mirroring
    `_supervise_once`, so it is unit-testable without a running FastAPI app.

    Own-scoping gate (2026-07-12, PNL-04/on-demand-reconcile follow-up): a
    `CorrectionRecord` WITHOUT `scope == "own"` is a LEGACY record, written
    before the OWN-01/OWN-03 fix, when this reconciler summed the operator's
    WHOLE shared account into "broker truth" (the real 2026-07-10 incident:
    it claims cash_delta -534.46 for a day the bot's own trade actually made
    +43.68). Such a record is not a resolution -- it is a stale artifact of
    the pre-fix bug, and `reporting/corrections.py` already refuses to
    render it. Treating its mere presence as "already reconciled" would
    permanently freeze that day on a polluted number with no way back in:
    the day must stay eligible for re-reconciliation (here, and via the
    on-demand endpoint below) until a genuine `scope="own"` record or a
    `DayBrokerConfirmed` actually resolves it.

    EOD-01 v1.59: when `broker_reads` is supplied (the live wiring passes
    the SAME `_BrokerReadFacade` the reconciler uses), settlement capture
    runs ONCE, BEFORE the reconcile compare -- so the bot's own numbers
    already include the broker-journaled settlement cash by the time they
    are checked against broker truth (see application/settlement_capture.py).
    `broker_reads=None` (every pre-v1.59 caller, and every test in
    tests/application/test_eod_reconcile_trigger.py) skips capture
    entirely -- unchanged behavior. A capture failure is swallowed exactly
    like the reconciler's own broker-unreachable case: it must never crash
    this tick, and the day simply stays uncaptured/unreconciled to retry
    next tick.

    2026-07-13 look-back fix (root cause: SPX 0DTE settlements post the day
    AFTER the trading day -- see settlement_capture.py's module docstring --
    so the ordinary same-day 16:15 capture above routinely finds nothing yet,
    and the `already` gate then seals the day FOREVER before its settlement
    ever posts). Above, the ordinary today-path is unchanged and still gated
    by `already`. Below, INDEPENDENTLY of that gate -- because the gate
    exists to stop redundant reconciles of a day whose facts haven't changed,
    never to freeze a day whose facts just changed -- every tick with
    `broker_reads` also re-checks the `lookback_days` (default 5, capped so
    this can never walk the whole journal, see `_settlement_lookback_days`)
    most recent PRIOR trading days. A prior day is only re-fetched from the
    broker at all if `_has_settlement_pending` (log-only, no broker call)
    says it still has an unresolved short with no captured settlement --
    a fully-settled prior day costs nothing here. `capture_settlements` is
    itself idempotent (keyed on `(at, symbol, sub_type)`), so re-running a
    day that still has nothing new simply reports zero captured and is left
    alone: only a day whose look-back capture actually appended a NEW
    `SettlementRecorded` gets re-reconciled, since that is the one whose
    bot-computed numbers just changed. Each prior day is captured/reconciled
    independently, under its own broad except, so one day's broker failure
    never blocks another's nor crashes the tick."""
    from meic.domain.events import CorrectionRecord, DayBrokerConfirmed
    from meic.reporting.folds import trading_days

    now = now_fn()
    if now.time() < EOD_RECONCILE_TIME:
        return
    day = now.date().isoformat()
    all_days = trading_days(comp.events)

    if day in all_days:
        already = any((isinstance(e, DayBrokerConfirmed) and e.date == day)
                      or (isinstance(e, CorrectionRecord) and e.date == day
                          and e.scope == "own")
                      for e in comp.events)
        if not already:
            if broker_reads is not None:
                from meic.application.settlement_capture import capture_settlements

                try:
                    await capture_settlements(comp.events, broker_reads, day,
                                              now_iso=lambda: now_fn().isoformat())
                except Exception:  # noqa: BLE001 -- never let a capture failure crash the tick
                    pass
            try:
                _mark_expired_sides(comp.events, day)
            except Exception:  # noqa: BLE001 -- never let marking crash the tick
                pass
            await reconciler.reconcile_day(day)

    if broker_reads is None:
        return  # pre-v1.59 caller / offline test -- no look-back possible either

    from meic.application.settlement_capture import capture_settlements

    prior_days = [d for d in all_days if d < day][-lookback_days:]
    for prior_day in prior_days:
        if _has_settlement_pending(comp.events, prior_day):
            try:
                result = await capture_settlements(comp.events, broker_reads, prior_day,
                                                   now_iso=lambda: now_fn().isoformat())
            except Exception:  # noqa: BLE001 -- one day's broker failure must not sink the tick
                continue
            if result.get("captured", 0) > 0:
                # This prior day's bot-computed numbers just changed (a real
                # settlement landed) -- re-reconcile it against broker truth,
                # deliberately bypassing the `already` gate above: that gate
                # guards the ORDINARY case (nothing changed), not this one.
                try:
                    await reconciler.reconcile_day(prior_day)
                except Exception:  # noqa: BLE001 -- never let a re-reconcile crash the tick
                    pass
        # EOD-01: mark any remaining side whose settlement has now landed --
        # log-only, so this runs whether the settlement was captured just
        # above THIS tick, or already sat captured (and unmarked) in the log
        # from before this marking step existed / from an earlier tick.
        try:
            _mark_expired_sides(comp.events, prior_day)
        except Exception:  # noqa: BLE001 -- never let marking crash the tick
            pass


def _journaled_own_order_ids(events) -> set[str]:
    """OWN-03: every broker order id the bot itself journaled placing — today
    `StopPlaced.broker_order_id` (v1.60), `DecayBuybackPlaced.broker_order_id`
    (v1.61), `LexOrderPlaced.broker_order_id` (v1.62) and
    `CondorFilled.broker_order_id` (entry order, OWN-01/OWN-03 fix), read
    generically off any event carrying the field. Delegates to the pure
    `reporting/own_orders.py::own_order_ids` — the ONE definition shared with
    `application/report_reconciler.py`, which cannot import this adapters
    module. The EOD-03 sweep cancels ONLY these: on a shared account
    (single-account operation is first-class, v1.49) the operator's own
    working orders are never touched and never flagged uncancellable.

    RESOLVED (v1.62, operator-ratified — the LEX-01 order-id journaling
    sub-bullet): the previously flagged known limit ("LEX-ladder orders
    journal no broker order ids") is closed. RecoverLong journals
    `LexOrderPlaced` at every placement — initial rung submit, every replace
    (each mints a new id), and the LEX-05 fallback — so LEX orders are now
    INCLUDED in the EOD-03 day-end order audit ("EOD-04's 'whatever remains
    expires' is unchanged for positions; this covers the ORDERS"). The one
    remaining non-journaled id is a live entry ladder's CURRENT working id,
    which the caller still merges in from the working-entry registry."""
    from meic.reporting.own_orders import own_order_ids

    return own_order_ids(events)


async def _maybe_eod_sweep_once(comp, now_fn, *, half_days: frozenset = frozenset()) -> None:
    """EOD-03: "All resting stop orders for positions that expired or were
    closed MUST be cancelled at EOD; the day does not end until the bot
    confirms zero working orders remain (or logs a critical alert naming each
    one it could not cancel)."

    Runs at/after the CALENDAR session close (DAY-02/DAY-01a: 13:00 ET on a
    half day — never a hardcoded 16:00), on a trading day with activity (the
    same RPT-01 gate `_maybe_eod_reconcile_once` uses), ONCE per day:
    journal-gated on `EodSweepCompleted`, so it is idempotent across ticks
    AND restarts. A sweep that completed with uncancellable orders already
    raised EOD-03's named critical alerts — the rule's own "or" clause — so
    it is complete and not re-run; a sweep that CRASHED (broker unreachable)
    journals nothing and retries next tick, exactly like the reconcile.

    Stop Trading (RSK-01) deliberately does NOT gate this: RSK-01 blocks new
    entries "and does nothing else", and cancelling day-end working orders is
    risk-reducing housekeeping EOD-03 makes unconditional. The raced-fill
    case (an order that FILLED while being cancelled) raises EndOfDaySweep's
    own distinct critical alert through `comp.alerts`. Factored out of the
    health tick, mirroring `_maybe_eod_reconcile_once`, so it is
    unit-testable without a running FastAPI app."""
    from meic.application.eod_sweep import EndOfDaySweep
    from meic.application.market_calendar import session_close
    from meic.domain.events import EodSweepCompleted
    from meic.reporting.folds import trading_days

    now = now_fn()
    if now.time() < session_close(now.date(), half_days=half_days):
        return
    day = now.date().isoformat()
    if day not in trading_days(comp.events):
        return  # no activity today -> no bot orders to sweep (RPT-01 gate)
    if any(isinstance(e, EodSweepCompleted) and e.date == day for e in comp.events):
        return  # already swept today (journal-gated; survives restart)

    own = _journaled_own_order_ids(comp.events)
    registry = getattr(comp, "working_entries", None)
    if registry is not None:
        own |= registry.order_ids()   # a live entry ladder's id is journaled nowhere
    result = await EndOfDaySweep(comp.broker, comp.alerts, own_order_ids=own).sweep()
    comp.events.append(EodSweepCompleted(
        date=day, cancelled=len(result.cancelled),
        uncancellable=len(result.uncancellable), raced_fills=len(result.raced_fills)))


# Terminal card states — no further P/L to estimate once here (matches the
# frontend's own TERMINAL list, EntryCards.tsx).
_TERMINAL_STATUSES = {"CLOSED", "EXPIRED", "DECAY_CLOSED"}


def _leg_mid(side_chain, strike: Decimal):
    """The current mid mark for `strike` on one ChainSide, or None if unmarked
    (far-OTM/no quote — the honest '—' case, never a fabricated number)."""
    if side_chain is None:
        return None
    mark = side_chain.marks.get(strike)
    return None if mark is None else mark.mid


def _streamer_symbol(snapshot, occ_symbol: str | None, side: str) -> str | None:
    """NFR-04 (2026-07-13, second pass): translate a journaled OCC leg symbol
    into the DXFEED STREAMER symbol DXLink actually speaks.

    THE BUG THIS FIXES (found live, 2026-07-13): the first cut of this wiring
    subscribed with the leg's OWN broker symbol (ORD-09 OCC form, e.g.
    "SPXW  260713C07575000"). DXLink does not know that namespace — it accepts
    the subscription, then silently sends NOTHING, which on the wire is
    indistinguishable from "no market data". The hub stayed permanently empty,
    `_resolve_leg_mid` correctly fell through to the snapshot every time, and
    the operator's mark went on ageing exactly as before. `streamer_pair`'s own
    docstring (adapters/dxlink/chain_snapshot.py) and
    tests/application/test_live_selection.py already record this trap; the
    subscription must use `.SPXW260713C7575`-form streamer symbols.

    The translation table is `ChainSnapshot.streamer_symbols` (strike ->
    (put, call), added alongside this fix — `snapshot_chain` already built it
    for its OWN quote collection and discarded it). NOTE for anyone tempted by
    `ChainSnapshot.symbols`: that map is the OCC pair (`occ_pair`, "what ORDERS
    name") — translating through it would be OCC->OCC, a silent no-op.

    Returns None — meaning "cannot translate, do not subscribe, do not look up"
    — when there is no snapshot yet, no streamer map, or the leg's strike is
    outside the subscribed span. The caller then falls back to the snapshot
    path exactly as it does today. A symbol string is NEVER guessed or
    reconstructed here: a wrong symbol is a silent no-quote, not an error."""
    if snapshot is None or not occ_symbol:
        return None
    table = getattr(snapshot, "streamer_symbols", None)
    if not table:
        return None
    try:
        strike = Decimal(_strike_from_symbol(occ_symbol))
    except (ArithmeticError, ValueError, IndexError):
        return None
    pair = table.get(strike)
    if pair is None:
        return None  # strike outside the subscribed span -> snapshot fallback
    put_sym, call_sym = pair
    return call_sym if side == "CALL" else put_sym


def _open_leg_symbols(events, snapshot) -> set[str]:
    """NFR-04 (2026-07-13): the STREAMER symbols (never OCC — see
    `_streamer_symbol`) of every leg on every currently-open entry: the
    QuoteHub stream task's subscription universe, and the SAME namespace the
    hub is keyed by, so a tick written by the stream is findable by the
    enricher. Reuses the SAME open/terminal test `_live_pnl_enricher` already
    applies (`_TERMINAL_STATUSES`) so the two never drift.

    A leg whose streamer symbol cannot be resolved (no snapshot yet, or a
    strike outside the subscribed span) is simply OMITTED — never guessed —
    so it keeps resolving off the snapshot, exactly as today."""
    from meic.domain.projection import fold

    day = fold(events)
    symbols: set[str] = set()
    for e in day.entries.values():
        if e.status in _TERMINAL_STATUSES or not e.legs:
            continue
        for leg in e.legs:
            streamer = _streamer_symbol(snapshot, leg.symbol, leg.side)
            if streamer:
                symbols.add(streamer)
    return symbols


def _resolve_leg_mid(occ_symbol: str | None, side: str, snapshot, strike: Decimal, *,
                     hub, now, max_quote_age_ms: int):
    """NFR-04 (2026-07-13): live-first mid resolution for one leg. `QuoteHub`
    and `DXLinkAdapter.quotes()` (doc 05 NFR-04) existed but were never wired
    into the live app, so `_live_pnl_enricher` and the shared TPF/TPT evaluator
    (`_open_side_costs`/`_entry_profit_pct_now`) both read marks off the chain
    snapshot refreshed only on the ~60s health-loop cadence — measured live
    2026-07-13 with the mark frozen while ageing past 50s.

    The hub is keyed by STREAMER symbol (the only namespace DXLink will send),
    so the leg's journaled OCC symbol is translated through `_streamer_symbol`
    before the lookup — one namespace end to end, no translation drift.

    STRICTLY NO WORSE than before: a fresh hub tick for this leg is preferred
    (LIVE, sub-second; `apply_tick`'s generation guard protects it from a
    zombie socket). A stale hub mark, an absent one, or a leg whose streamer
    symbol cannot be resolved at all, is treated as ABSENT — it falls through
    to the EXACT snapshot path this replaces (`_leg_mid`); if that is also
    unmarked, the result is an honest None, never a guess. If `hub` or `now`
    is not supplied (paper mode, or any caller that predates this wiring), the
    hub step is skipped entirely and behaviour is byte-identical to before.

    Returns `(mid, hub_stamp)` — `hub_stamp` is the HUB quote's own
    `stamped_at` when the mark came from the hub, else `None` (the caller
    uses this to decide whether `live_pnl_asof` may honestly claim a live
    timestamp)."""
    side_chain = None
    if snapshot is not None:
        side_chain = snapshot.put_side if side == "PUT" else snapshot.call_side
    if hub is not None and now is not None:
        streamer = _streamer_symbol(snapshot, occ_symbol, side)
        if streamer:
            q = hub.mark(streamer)
            if q is not None and not q.is_stale(now, max_quote_age_ms):
                return q.mid, q.stamped_at
    return _leg_mid(side_chain, strike), None


def _live_pnl_enricher(comp, snaps, hub=None, *, clock=None, max_quote_age_ms: int = 3000):
    """FEATURE 3: live P/L on OPEN entry cards, from the chain snapshot already
    held for selection/DAT-02 — no new subscription. Reads `snaps.last`, so it
    updates on the same ~60s health-loop cadence that refreshes it (see
    `_wire_live_day`/`_probe_once`); a mark outside the ATM band, or a stale/
    absent snapshot, yields an honest null ("—" in the UI) rather than a guess.

    BUG FIX (2026-07-13, live incident): this used to re-mark ALL FOUR legs as
    if the whole condor were still open, ignoring `stop_fills`/`recoveries`/
    `fees` entirely — so the instant a side was stopped and closed, the number
    priced a spread the bot no longer owned (observed: a stopped+LEX-recovered
    PUT side re-marked at its now-meaningless price, once even on options that
    had EXPIRED a week earlier). The correct figure already existed in the
    shared TPF/TPT evaluator (`_entry_profit_pct_now`/`domain.tpf`), which
    folds `stop_fills`/`recoveries`/`fees` and marks ONLY the still-open
    sides. This enricher now derives `live_pnl` from the SAME per-share
    quantity (`domain.tpf.entry_profit_amount`) fed by the SAME open-side
    costing (`_open_side_costs`) that evaluator uses — one formula, two
    consumers (RPT-12/TPF-01), so `live_pnl` and `profit_pct` can never
    diverge again. A fully-closed entry (every side stopped/closed/expired)
    needs no mark at all and still produces a real number; only a STILL-OPEN
    side with no available mark yields the honest null.

    NFR-04 (2026-07-13): `hub`/`clock`, when supplied, let each open side's
    mid resolve to a LIVE QuoteHub tick instead of the snapshot's (up to ~60s
    old) value — see `_resolve_leg_mid` (via `_open_side_costs`). `live_pnl_asof`
    only claims the live/hub timestamp when EVERY mark that actually
    contributed (i.e. every still-open side's legs) resolved live this tick;
    if even one fell back to the snapshot — or nothing needed a mark at all —
    the card's `asof` stays the snapshot's own `taken_at`, exactly today's
    behaviour, never a misleading "live" stamp. `hub` absent/empty/sick (no
    marks land) reduces byte-for-byte to the pre-NFR-04 snapshot-only path.
    """
    from meic.domain.projection import fold
    from meic.domain.tpf import entry_profit_amount

    def enrich(cards: list[dict]) -> list[dict]:
        snap = getattr(snaps, "last", None)
        now = clock.now() if clock is not None else None
        day = fold(comp.events)
        for card in cards:
            card["live_pnl"] = None
            card["live_pnl_asof"] = None
            if card.get("status") in _TERMINAL_STATUSES:
                continue
            if snap is None or snap.stale:
                continue
            e = day.entries.get(card["entry_id"])
            if e is None or not e.legs:
                continue
            stamps: dict[str, tuple] = {}
            open_costs = _open_side_costs(e, snap, hub=hub, now=now,
                                          max_quote_age_ms=max_quote_age_ms, stamps=stamps)
            if open_costs is None:
                continue  # a still-open side's mark is unavailable -> honest null, never a guess
            contracts = next((leg.qty for leg in e.legs if leg.role == "short"), 1)
            profit = entry_profit_amount(net_credit=e.net_credit, fees=e.fees, stop_fills=e.stop_fills,
                                         recoveries=e.recoveries, open_side_costs=open_costs)
            card["live_pnl"] = str(profit * 100 * contracts)
            hub_stamps = [s for pair in stamps.values() for s in pair]
            if hub_stamps and all(s is not None for s in hub_stamps):
                card["live_pnl_asof"] = max(hub_stamps).isoformat()  # every contributing mark LIVE
            else:
                card["live_pnl_asof"] = snap.taken_at.isoformat()    # any fallback (or nothing open) -> today's stamp
        return cards

    return enrich


def _profit_pct_enricher(comp, snaps, hub=None, *, clock=None, max_quote_age_ms: int = 3000):
    """UI-13/14/15: the entry card's current profit% (TPF-01/TPT-01's shared
    evaluator), off the SAME held snapshot `_live_pnl_enricher` reads — live
    only; paper cards get `profit_pct: None` (no live chain marks, honest
    absence rather than a guess, matching FEATURE 3's own convention).

    NFR-04 (2026-07-13): `hub`/`clock`, passed through to `_entry_profit_pct_now`,
    let the SAME evaluator TPF/TPT uses prefer a live QuoteHub mark per leg —
    see `_resolve_leg_mid`."""
    from meic.domain.projection import fold

    def enrich(cards: list[dict]) -> list[dict]:
        snap = getattr(snaps, "last", None)
        now = clock.now() if clock is not None else None
        day = fold(comp.events)
        for card in cards:
            e = day.entries.get(card["entry_id"])
            pct = None if e is None else _entry_profit_pct_now(
                e, snap, hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)
            card["profit_pct"] = None if pct is None else str(pct)
        return cards

    return enrich


def _open_side_costs(e, snapshot, *, hub=None, now=None, max_quote_age_ms: int = 3000,
                     stamps: dict[str, tuple] | None = None) -> dict[str, Decimal] | None:
    """The current cost-to-close (short mid − long mid) for each still-OPEN
    side of entry `e` — the per-share input `domain.tpf.entry_profit_pct`/
    `entry_profit_amount` needs for their "unrealized P&L of open sides at
    mid" term. Shares the same per-leg mid derivation as
    `_live_pnl_enricher`/`_sample_marks_once` above, restricted to sides not
    yet stopped/closed/expired (TPF-05: a resolved side contributes its
    REALIZED effect only, already inside stop_fills/recoveries — never
    re-marked here).

    NFR-04 (2026-07-13): each leg resolves through `_resolve_leg_mid` —
    hub-first, snapshot-fallback — instead of `_leg_mid` directly. `hub`/`now`
    default to None, which skips the hub step entirely (byte-identical to the
    pre-NFR-04 snapshot-only behaviour).

    `stamps`, if supplied, is filled in-place with `side -> (short_hub_stamp,
    long_hub_stamp)` for every OPEN side actually costed — so a caller that
    needs to know whether those marks came from a live hub tick (e.g.
    `_live_pnl_enricher`'s `live_pnl_asof`) can ask without a second,
    drifting mark-resolution pass. Optional and additive: every existing
    caller that doesn't pass it is unaffected.

    Returns None (an honest gap, DAT-02) when any open side cannot be FULLY
    marked (missing legs, or a leg outside the ATM band with no quote) — the
    caller treats that exactly like a stale snapshot: pause, never guess.
    """
    gone = set(e.sides_stopped) | set(e.sides_closed) | set(e.sides_expired)
    by_side: dict[str, dict] = {"PUT": {}, "CALL": {}}
    for leg in e.legs:
        by_side.setdefault(leg.side, {})[leg.role] = leg
    out: dict[str, Decimal] = {}
    for side in ("PUT", "CALL"):
        if side in gone:
            continue
        short_leg, long_leg = by_side[side].get("short"), by_side[side].get("long")
        if short_leg is None or long_leg is None:
            return None
        short_mid, short_at = _resolve_leg_mid(
            short_leg.symbol, side, snapshot, Decimal(_strike_from_symbol(short_leg.symbol)),
            hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)
        long_mid, long_at = _resolve_leg_mid(
            long_leg.symbol, side, snapshot, Decimal(_strike_from_symbol(long_leg.symbol)),
            hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)
        if short_mid is None or long_mid is None:
            return None
        out[side] = short_mid - long_mid
        if stamps is not None:
            stamps[side] = (short_at, long_at)
    return out


def _entry_profit_pct_now(e, snapshot, *, hub=None, now=None, max_quote_age_ms: int = 3000):
    """The shared TPF-01/TPT-01 evaluator, fed live marks — None (stale/
    unmarked/no-credit-yet) means "unknown", never a guess.

    NFR-04 (2026-07-13): the OUTER snapshot-presence/staleness gate below is
    UNCHANGED from before this wiring — whether evaluation is attempted AT ALL
    still depends only on the chain snapshot, exactly as today. `hub`/`now`
    only change the SOURCE of each leg's mid once evaluation proceeds (see
    `_open_side_costs` -> `_resolve_leg_mid`), so a hub that is absent, empty
    or sick leaves this function byte-identical to before."""
    from meic.domain.tpf import entry_profit_pct

    if snapshot is None or snapshot.stale:
        return None
    open_costs = _open_side_costs(e, snapshot, hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)
    if open_costs is None:
        return None
    return entry_profit_pct(net_credit=e.net_credit, fees=e.fees, stop_fills=e.stop_fills,
                            recoveries=e.recoveries, open_side_costs=open_costs)


def _profit_pct_provider(comp, snapshots, hub=None, *, clock=None, max_quote_age_ms: int = 3000):
    """PanelCommands' TPF-02/TPT-03 gap-validation hook: current profit% for
    one entry, off the SAME evaluator and the SAME held snapshot the health
    tick reads — never a second, drifting computation. NFR-04: same hub-aware
    resolution as `_profit_pct_enricher` above."""
    from meic.domain.projection import fold

    def provider(entry_id: str):
        e = fold(comp.events).entries.get(entry_id)
        if e is None:
            return None
        now = clock.now() if clock is not None else None
        return _entry_profit_pct_now(e, snapshots.last, hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)

    return provider


async def _evaluate_exits_once(comp, snapshot, exit_monitor, commands, *,
                               hub=None, clock=None, max_quote_age_ms: int = 3000) -> None:
    """TPF/TPT health-tick evaluation (TPF-03/TPT-04): bot-side only, NEVER
    broker-resting. Stale marks (or an unmarked open side) pause evaluation
    (DAT-02) — the confirmation counters reset rather than fire on a gap.
    TPT-05: any stop fill on the entry disarms its target PERMANENTLY, so the
    target is never evaluated once `e.sides_stopped` is non-empty.

    NFR-04 (2026-07-13): `hub`/`clock` let `_entry_profit_pct_now` prefer a
    live mark per leg; the top-level `stale` gate below (whether evaluation is
    attempted at all) is UNCHANGED."""
    from meic.domain.projection import fold

    floors, targets = comp.state.tpf_floors, comp.state.tp_targets
    if not floors and not targets:
        return
    day = fold(comp.events)
    stale = snapshot is None or snapshot.stale
    now = clock.now() if clock is not None else None
    for entry_id, e in day.entries.items():
        level_floor, level_target = floors.get(entry_id), targets.get(entry_id)
        if level_floor is None and level_target is None:
            continue
        if e.status in _TERMINAL_STATUSES:
            exit_monitor.forget(entry_id)
            continue
        profit_pct = None if stale else _entry_profit_pct_now(
            e, snapshot, hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)
        entry_stale = stale or profit_pct is None

        if level_floor is not None:
            if exit_monitor.evaluate_floor(entry_id, profit_pct=profit_pct,
                                           level=int(level_floor), stale=entry_stale):
                await commands.close_as(entry_id, "take_profit")
                continue  # the entry is now closed — skip its target this tick

        if level_target is not None:
            if e.sides_stopped:  # TPT-05: permanent disarm
                exit_monitor.disarm_target(entry_id)
            elif exit_monitor.evaluate_target(entry_id, profit_pct=profit_pct,
                                              level=int(level_target), stale=entry_stale):
                await commands.close_as(entry_id, "take_profit_target")


async def _recover_exits_once(comp, snapshot, commands, *,
                              hub=None, clock=None, max_quote_age_ms: int = 3000) -> None:
    """TPF-08/TPT-07: on recovery (boot/reconnect), an already-breached floor
    or an already-reached target fires IMMEDIATELY — no confirmation streak,
    because the bot was down while it happened; the realized level may be
    worse (floor) or better (target) than armed, inherent to bot-side
    monitoring and shown in the day report. Must only be called AFTER
    `_boot_reconcile()` has appended any synthesized stop events, so TPT-05's
    disarm applies BEFORE this check (TPT-07's recovery-order rule) — a
    disarmed target here already reads `e.sides_stopped` non-empty.

    NFR-04 (2026-07-13): `hub`/`clock` passed through to `_entry_profit_pct_now`
    for the same live-first/snapshot-fallback resolution; the top-level
    snapshot gate below is UNCHANGED."""
    from meic.domain.projection import fold
    from meic.domain.tpf import breached
    from meic.domain.tpt import reached

    if snapshot is None or snapshot.stale:
        return
    floors, targets = comp.state.tpf_floors, comp.state.tp_targets
    if not floors and not targets:
        return
    day = fold(comp.events)
    now = clock.now() if clock is not None else None
    for entry_id, e in day.entries.items():
        if e.status in _TERMINAL_STATUSES:
            continue
        level_floor, level_target = floors.get(entry_id), targets.get(entry_id)
        if level_floor is None and level_target is None:
            continue
        profit_pct = _entry_profit_pct_now(e, snapshot, hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)
        if profit_pct is None:
            continue
        if level_floor is not None and breached(Decimal(level_floor), profit_pct):
            await commands.close_as(entry_id, "take_profit")
            continue
        if (level_target is not None and not e.sides_stopped
                and reached(Decimal(level_target), profit_pct)):
            await commands.close_as(entry_id, "take_profit_target")


def _sample_marks_once(comp, snapshot) -> None:
    """RPT-12/D8 (doc 10): one EntryMarkSample per OPEN entry, journaled at
    the health-tick cadence, from the SAME chain snapshot `_live_pnl_enricher`
    reads (no new subscription). Reuses that function's open-entry test
    (`_TERMINAL_STATUSES`) and per-side leg derivation so the two never drift.

    A missing or stale snapshot samples NOTHING this tick — same honesty rule
    `_live_pnl_enricher` already applies (a maybe-wrong mark is worse than a
    gap; D10 wants gaps, never fabrication). An open entry where every mark
    AND spot come back absent appends nothing either (no all-None sample);
    otherwise one EntryMarkSample is appended per open entry, with each mark
    field independently None where its leg's strike has no quote.
    """
    from meic.domain.events import EntryMarkSample
    from meic.domain.projection import fold

    if snapshot is None or snapshot.stale:
        return
    at = getattr(snapshot, "taken_at", None)
    at_iso = at.isoformat() if at is not None else None
    day = fold(comp.events)
    for e in day.entries.values():
        if e.status in _TERMINAL_STATUSES or not e.legs:
            continue
        by_side: dict[str, dict] = {"PUT": {}, "CALL": {}}
        for leg in e.legs:
            by_side.setdefault(leg.side, {})[leg.role] = leg
        put_short, put_long = by_side["PUT"].get("short"), by_side["PUT"].get("long")
        call_short, call_long = by_side["CALL"].get("short"), by_side["CALL"].get("long")
        put_short_mid = (_leg_mid(snapshot.put_side, Decimal(_strike_from_symbol(put_short.symbol)))
                         if put_short else None)
        put_long_mid = (_leg_mid(snapshot.put_side, Decimal(_strike_from_symbol(put_long.symbol)))
                        if put_long else None)
        call_short_mid = (_leg_mid(snapshot.call_side, Decimal(_strike_from_symbol(call_short.symbol)))
                          if call_short else None)
        call_long_mid = (_leg_mid(snapshot.call_side, Decimal(_strike_from_symbol(call_long.symbol)))
                        if call_long else None)
        spot = getattr(snapshot, "spot", None)
        if spot is None and all(m is None for m in (put_short_mid, put_long_mid,
                                                     call_short_mid, call_long_mid)):
            continue  # nothing honest to record this tick — no fabricated all-None sample
        comp.events.append(EntryMarkSample(
            entry_id=e.entry_id, at=at_iso, spot=spot,
            put_short_mid=put_short_mid, put_long_mid=put_long_mid,
            call_short_mid=call_short_mid, call_long_mid=call_long_mid))


async def _stream_open_entry_quotes(comp, hub, feed, snaps, *, idle_seconds: float = 5.0) -> None:
    """NFR-04 (2026-07-13): one subscribe-and-apply pass over the CURRENT open
    entries' legs, feeding `hub.apply_tick` off the SAME `MarketDataFeed.quotes()`
    seam the chain snapshot already uses under the hood —
    `QuoteHub`/`DXLinkAdapter.quotes()` existed but were never wired together
    before this change.

    Subscribes by STREAMER symbol, never the journaled OCC one (see
    `_streamer_symbol`: DXLink silently sends NOTHING for an OCC subscription,
    which is exactly why the first cut of this loop left the hub permanently
    empty). The translation needs the held chain snapshot, hence `snaps`.

    Idles `idle_seconds` and returns (never raises) when there is nothing to
    subscribe to yet — no open entries, or no snapshot to translate their
    symbols through — since either can arrive at any moment and the caller
    simply calls this again. Returns (without raising) as soon as the
    subscribable set CHANGES (a new entry filled, one closed, or a refreshed
    snapshot changed the strike->streamer map) so the caller re-subscribes with
    the fresh set. Any streaming failure PROPAGATES to the caller, which owns
    the try/except/backoff (`_run_quote_stream_loop` below) — kept thin here so
    the subscribe/re-subscribe logic is unit-testable on its own with a fake
    feed.
    """
    symbols = _open_leg_symbols(comp.events, getattr(snaps, "last", None))
    if not symbols:
        await asyncio.sleep(idle_seconds)
        return
    gen = hub.open_generation()
    async for q in feed.quotes(sorted(symbols)):
        hub.apply_tick(q, generation=gen)
        # Recomputed against the CURRENT snapshot every tick: a refreshed
        # snapshot can change the strike->streamer map, not just the open book.
        if _open_leg_symbols(comp.events, getattr(snaps, "last", None)) != symbols:
            return  # the subscribable set changed -- re-subscribe with the new one


async def _run_quote_stream_loop(comp, hub, feed, snaps, alerts, *, idle_seconds: float = 5.0,
                                 retry_seconds: float = 5.0, connected=lambda: True) -> None:
    """NFR-04 (2026-07-13): supervises `_stream_open_entry_quotes` forever, so
    live P/L (`_live_pnl_enricher`) and the shared TPF/TPT evaluator
    (`_entry_profit_pct_now`) can read a QuoteHub mark that ticks live instead
    of the chain snapshot's ~60s health-loop cadence — measured live
    2026-07-13 with the mark frozen while ageing past 50s. Operator-requested,
    deployed mid-session under supervision.

    NEVER crashes the app or the health loop: any failure (including the feed
    simply not being connected yet) marks the hub sick and backs off; the
    enrichers' snapshot fallback (`_resolve_leg_mid`) keeps the panel exactly
    as good as it was before this wiring existed. `connected` gates streaming
    on the broker session being up (same shape as the other startup loops in
    `live_app`); while not connected this just idles, same as the
    nothing-to-subscribe-to case."""
    while True:
        if not connected():
            await asyncio.sleep(idle_seconds)
            continue
        try:
            await _stream_open_entry_quotes(comp, hub, feed, snaps, idle_seconds=idle_seconds)
        except Exception as exc:  # noqa: BLE001 -- must never crash the app
            hub.mark_sick()
            alerts.alert("warning", f"NFR-04 quote stream failed: {exc!r}")
            await asyncio.sleep(retry_seconds)


async def _stop_watchdog_pass(comp, wd, hub, snaps, *, now, max_quote_age_ms: int,
                              last_ticked: dict) -> None:
    """STP-03b (2026-07-13): one pass of the stop watchdog over every OPEN
    short with a resting stop placed -- `_open_short_legs`, the SAME EC-STP-06
    frame the stop-fill catch-up loop (`stop_fill_watch.py`) already drives,
    so this loop and that one agree on exactly what "open with a stop placed"
    means. Feeds `StopWatchdog.observe` a LIVE QuoteHub mark, translated
    through the SAME `_streamer_symbol` seam the P&L path uses
    (`_resolve_leg_mid`) -- one mark source end to end, no drift.

    `last_ticked` (owned by the caller, persists across passes) is this
    function's own wall-clock bookkeeping: `seconds_since_last` is the REAL
    time elapsed since the last observation of this (entry_id, side), whether
    that prior observation was fresh or stale. DAT-02 pause/resume itself is
    entirely `StopWatchdog.observe`'s job (it ignores the elapsed gap while
    `stale=True`); this loop's only job is reporting the real gap honestly. A
    side observed for the first time gets 0 elapsed seconds -- it is never
    credited with breach time that occurred before the watchdog was watching.

    The trigger and the resting stop's own broker order id come from the
    JOURNALED `StopPlaced` (REC-02: the log is authoritative for intent) --
    never recomputed. A short whose mark cannot be resolved this tick (no
    streamer symbol yet, no hub tick at all, or one older than
    `max_quote_age_ms`) is fed `stale=True`, pausing the breach clock exactly
    as a stale chain-snapshot mark would (DAT-02)."""
    from meic.application.stop_fill_watch import _open_short_legs

    snap = getattr(snaps, "last", None)
    seen: set[tuple[str, str]] = set()
    for entry_id, side, leg, spec in _open_short_legs(comp.events):
        key = (entry_id, side)
        seen.add(key)
        # REC-02: the resting stop's own broker order id, straight off the
        # journaled StopPlaced -- the ORD-08 race pre-check inside escalate()
        # re-checks THIS id against broker truth immediately before submitting.
        wd.resting_stop_ids[key] = spec.broker_order_id
        elapsed = now - last_ticked[key] if key in last_ticked else timedelta(0)
        last_ticked[key] = now

        streamer = _streamer_symbol(snap, leg.symbol, side)
        quote = hub.mark(streamer) if streamer else None
        stale = quote is None or quote.is_stale(now, max_quote_age_ms)
        mark = quote.mid if quote is not None else Decimal("0")

        action = wd.observe(
            entry_id=entry_id, side=side, mark=mark, trigger=spec.trigger,
            seconds_since_last=Decimal(str(elapsed.total_seconds())),
            stop_filled=False, stale=stale)
        if action == "escalate" and quote is not None:
            await wd.escalate(entry_id=entry_id, side=side, mark_at_breach=mark,
                              ask=quote.ask, symbol=leg.symbol, contracts=leg.qty)

    # A side that left the open-short-with-a-stop frame (stopped/closed/
    # expired, or the stop itself no longer on record) has nothing left to
    # accumulate -- drop its bookkeeping so a LATER, unrelated short reusing
    # the same (entry_id, side) key (impossible today, defensive regardless)
    # never inherits a stale elapsed baseline.
    for key in set(last_ticked) - seen:
        last_ticked.pop(key, None)


async def _run_stop_watchdog_loop(comp, wd, hub, snaps, alerts, *, clock,
                                  max_quote_age_ms: int, idle_seconds: float = 5.0,
                                  connected=lambda: True) -> None:
    """STP-03b (2026-07-13): supervises `_stop_watchdog_pass` forever -- same
    shape as `_run_quote_stream_loop` above (one supervised background task,
    created unconditionally at startup, cancelled on shutdown). NEVER crashes
    the app: any failure (a broker hiccup during `escalate()`, a momentarily
    absent snapshot, anything) is alerted and the loop simply tries again next
    tick -- a missed pass is never worse than the watchdog not existing at
    all, and the resting broker stop stays PRIMARY and bot-independent
    regardless of this loop's health."""
    last_ticked: dict[tuple[str, str], datetime] = {}
    while True:
        if not connected():
            await asyncio.sleep(idle_seconds)
            continue
        try:
            await _stop_watchdog_pass(comp, wd, hub, snaps, now=clock.now(),
                                      max_quote_age_ms=max_quote_age_ms,
                                      last_ticked=last_ticked)
        except Exception as exc:  # noqa: BLE001 -- must never crash the app
            alerts.alert("warning", f"STP-03b stop watchdog pass failed: {exc!r}")
        await asyncio.sleep(idle_seconds)


def _wire_live_day(comp, env: dict[str, str]) -> dict:
    """Assemble the live trading day: selector, gates, runtime, ▶, pre-flight.

    Thin: every decision that could leave a SAFETY RAIL unarmed lives in
    composition/live_wiring.py, where tests/composition/test_live_wiring.py asserts
    on it directly. That test exists because this function's predecessor built a
    LiveRuntime with max_day_risk, order_cap and buying_power all left at None,
    and threw the composed schedule rows away — while the paper composition and
    every unit test had all of it armed.
    """
    from meic.application.timeouts import run_warmup
    from meic.application.warmup import ALERT_AT_SECONDS
    from meic.composition.live_gates import LiveMarketGates
    from meic.composition.live_selection import LiveCondorSelector, SelectionConfig
    from meic.composition.live_selection import floor_candidates as floor_candidates_fn
    from meic.composition.live_wiring import (
        BrokerClockProbe,
        build_live_runtime,
        build_manual_entry,
        live_preflight_checks,
    )
    from meic.domain.quote_hub import QuoteHub

    min_buying_power = Decimal(env.get("MEIC_MIN_BUYING_POWER", "5000"))
    max_drift_ms = float(env.get("MEIC_MAX_CLOCK_DRIFT_MS", "2000"))   # DAY-03 v1.48

    # NFR-04 (2026-07-13): the persistent, generation-guarded marks table the
    # live quote-stream loop (`_run_quote_stream_loop`, wired in `live_app`)
    # writes and the enrichers below read live-first, snapshot-fallback. Built
    # here (not in `live_app`) so `tests/composition/test_live_wiring.py` can
    # assert on it directly, matching every other safety-relevant object this
    # function returns.
    quote_hub = QuoteHub()
    max_quote_age_ms = _max_quote_age_ms(env)

    # DAY-03 (v1.48): drift is measured against the BROKER's Date header on the
    # ~60 s session probe — no env var, no NTP. Starts unverified (infinite drift),
    # so entries are blocked until the first probe lands; a reading older than 300 s
    # is treated as unverified too. The session probe below feeds it.
    drift = BrokerClockProbe()

    class _Snapshots:
        """Freshness of the most recent chain snapshot, so the DAT-02 gate — and
        the UC-02 pre-flight — reflect the data the selector actually used.
        Starts STALE: unknown freshness is never 'fresh'. Also holds the snapshot
        ITSELF (`.last`) — FEATURE 3 (live P/L card) reads marks off it directly
        rather than opening a second subscription; it refreshes on the same ~60s
        health-loop cadence as everything else that reads `.stale`."""
        stale = True
        last = None

        async def take(self):
            from meic.adapters.dxlink.chain_snapshot import snapshot_chain
            # v1.51: no band_points — the subscription span is an internal
            # constant (SUBSCRIBE_SPAN_PTS); the STK-10 gate is trade-relative.
            snap = await snapshot_chain(comp.broker._session)
            self.stale = snap.stale
            self.last = snap
            return snap

    snaps = _Snapshots()

    async def _data_fresh() -> bool:
        return not snaps.stale

    async def _session_valid() -> bool:
        # The ~60 s session probe (NFR-02) doubles as the DAY-03 clock reading:
        # the broker's Date header on THIS response is the drift source (v1.48).
        await comp.broker.working_orders()  # a light authenticated call; raises if dead
        drift.record(await comp.broker.server_time())
        return True

    async def _buying_power_ok() -> bool:
        return (await comp.broker.buying_power()) >= min_buying_power

    selector = LiveCondorSelector(
        snapshot_provider=snaps.take,
        # STK-10: the chain-scoped completeness dial (doc 06, 50-100, default 90),
        # previously hardcoded at 90 inside SelectionConfig.
        config=SelectionConfig(completeness_pct=_chain_completeness_pct(env),
                               min_validated_strikes=_min_validated_strikes(env)),
        # STK-10 v1.51 retry: comp.clock drives the retry gaps (never time.sleep —
        # this is the SAME clock LiveRuntime schedules entries against), bounded
        # by the entry window from `when` (doc 06 entry_window_seconds/
        # chain_retry_seconds, both env-wired like every other live tunable).
        clock=comp.clock,
        entry_window_seconds=_entry_window_seconds(env),
        chain_retry_seconds=_chain_retry_seconds(env),
        # STK-10 v1.55: baseline pre-validation is ALWAYS ON for real trading
        # (both the scheduled runtime and manual ENT-09 fire cross the SAME
        # selector instance below, so "at warm-up" / "at press" both land on
        # whichever call reaches this selector first for that entry).
        baseline_pre_validation=True,
        alert=comp.alerts.alert)
    # DAY-01/02 (operator ruling 2026-07-11): the exchange calendar the ENT-03
    # market-open gate consults — previously wired with the dataclass default
    # (an EMPTY set), so market holidays looked like open days. The rules are
    # exchange facts computed algorithmically (nyse_holidays.py), not operator
    # config; a decade out costs nothing and outlives any realistic uptime.
    # DAY-01a (v1.61): construct through the guarded LIVE seam — an empty
    # calendar at boot is a construction error, never an open market.
    # DAY-03: anchor on the ET trading day (correct by construction), not a
    # UTC boot-time date that could name the wrong year right at a New Year's
    # Eve boundary (UTC rolls to Jan 1 hours before ET does).
    _cal_anchor = trading_day(comp.clock.now())
    gates = LiveMarketGates.for_live(clock=comp.clock, data_fresh=_data_fresh,
                                     session_valid=_session_valid, buying_power_ok=_buying_power_ok,
                                     holidays=holidays_near(_cal_anchor, years_ahead=10),
                                     half_days=half_days_near(_cal_anchor, years_ahead=10))

    lead_seconds = _warmup_lead_seconds(env)   # ENT-08 session_warmup_lead_seconds

    async def _entry_warmup(when: datetime, entry_number: int,
                            config: SelectionConfig | None) -> None:
        """ENT-08 (operator ruling 2026-07-11): real T-60 warm-up wiring,
        reusing ONLY the existing probe/snapshot machinery -- no new
        streaming infrastructure is built here.

          1/2. token validity + account-stream heartbeat -> the SAME
               `_session_valid` the ~60s health loop already runs (a light
               authenticated call; the SDK `Session` under `comp.broker`
               renews its own access token on any authenticated call it
               makes, exactly as it does for every other call this process
               issues -- there is no separate adapter-level "seconds until
               expiry" reader available to drive an INDEPENDENT
               `session_token_expiry_buffer_seconds` timer here; this is a
               known scope boundary of the existing machinery, not silently
               faked).
          3.   DXLink chain subscription freshness -> the SAME `snaps.take()`
               the selector itself uses at fire time; a freshly-taken
               snapshot IS a live, ticking subscription -- there is nothing
               further to "resubscribe" beyond taking a fresh one.
          4.   hard wall-clock cap (NFR-03, `timeouts.run_warmup`), bounded so
               it can never run past `ALERT_AT_SECONDS` before the entry --
               the clock must never slip (ENT-08). Still-unresolved at the
               cap raises a critical alert (ENT-08.4) rather than silently
               proceeding.

        STK-10 v1.55 hook: once a fresh snapshot is in hand, locks THIS
        entry's validated-universe baseline under the SAME (when,
        entry_number) key the fire will use
        (`LiveCondorSelector.warm_baseline`) -- so fire-time completeness
        measures regression from a T-60 picture instead of approximating the
        capture lazily at the first fire-time attempt.
        """
        async def _prime() -> None:
            try:
                await _session_valid()
            except Exception as exc:  # noqa: BLE001 -- warm-up never crashes the scheduler
                comp.alerts.alert("warning", f"ENT-08 warm-up session probe failed: {exc!r}")
            try:
                await snaps.take()
            except Exception as exc:  # noqa: BLE001
                comp.alerts.alert("warning", f"ENT-08 warm-up chain probe failed: {exc!r}")
            selector.warm_baseline(snaps.last, config, when=when, entry_number=entry_number)

        cap_seconds = max(0.0, lead_seconds - ALERT_AT_SECONDS)
        completed, _ = await run_warmup(_prime(), cap_seconds=cap_seconds)
        if not completed:
            comp.alerts.alert(
                "critical",
                f"ENT-08 warm-up capped at T-{ALERT_AT_SECONDS:.0f}s for entry "
                f"#{entry_number} at {when.isoformat()} -- firing on schedule regardless")

    # RSK-04 + RSK-08 + ENT-03 BP, all armed. Also wraps comp.broker so the order
    # cap counts every order any service submits.
    runtime = build_live_runtime(comp, selector=selector, market_gates=gates,
                                 warmup=_entry_warmup, warmup_lead_seconds=lead_seconds,
                                 max_entries_per_day=_max_entries(comp),
                                 drift=drift, max_clock_drift_ms=max_drift_ms)

    # ENT-09: the panel's ▶ crosses the identical rails (same ceiling, same book).
    manual = build_manual_entry(
        comp, selector=selector, market_gates=gates,
        max_entries_per_day=_max_entries(comp), drift=drift, max_clock_drift_ms=max_drift_ms,
        # DAY-03 (THE confirmed live bug, 2026-07-13): this used to be
        # `datetime.now(timezone.utc).astimezone().date().isoformat()`, which
        # converts to the SYSTEM's local timezone (whatever the operator's
        # machine happens to be set to) -- not ET. A BST operator's local
        # midnight (7pm ET) or a Tokyo operator's local midnight (11am ET,
        # MID-SESSION) silently stamped the wrong trading day onto every
        # entry_id this manual/ad-hoc lane fires, and onto /entries' day-scope
        # filter (`commands.day()` reads the SAME "today" via `self.today()`
        # below) -- a real cert trade vanished from the board this way live.
        # `trading_day_str` is the ONE shared ET derivation (application/
        # market_calendar.py) -- never a second ZoneInfo/astimezone call.
        day=lambda: trading_day_str(comp.clock.now()),
        # ENT-09b v1.57 refuse-and-re-pick: the live spot off the SAME cached
        # snapshot FEATURE 3 already holds -- no new subscription.
        spot_provider=lambda: getattr(snaps.last, "spot", None))

    # TPF/TPT (v1.58): ONE ExitMonitor for the whole live day, held here (not
    # per-tick) so its per-entry confirmation counters survive across health
    # ticks — the same reason `snaps` itself is held rather than rebuilt.
    from meic.application.exit_monitor import ExitMonitor

    exit_monitor = ExitMonitor()

    async def _long_quote(long_symbol: str, side: str):
        """EC-STP-06 catch-up (v1.60): the live market data RecoverLong's
        ladder needs to start, off the SAME chain snapshot FEATURE 3 already
        holds (`snaps.last`) — no new subscription. Returns one of:

          * `None` — nothing can be priced this tick: no snapshot yet, or
            no spot at all (EC-LEX-08 case (c) — no underlying mark means no
            intrinsic floor is computable either) — the caller retries next
            tick, never guesses.
          * `NoBidFloor` (EC-LEX-08 v1.63, case (a)) — the strike itself
            carries no bid, but spot is present and DAT-02-fresh: the LEX-04
            intrinsic floor is computable, so the caller can rest a floor
            sell instead of deferring forever.
          * `StaleQuote` (STP-08a v1.62) — a bid EXISTS but the snapshot is
            too old to price a ladder (LEX-02's age criterion); after the
            bounded `lex_quote_wait_seconds` deferral it can still price the
            LEX-05 marketable-at-bid fallback — the freshest bid the system
            has.
          * `(Quote, intrinsic)` — a fresh, priceable quote.
        """
        from meic.application.stop_fill_watch import NoBidFloor, StaleQuote
        from meic.domain.ladder import intrinsic_call, intrinsic_put

        snap = snaps.last
        if snap is None:
            return None
        # Decimal, NOT the raw string `_strike_from_symbol` returns:
        # `ChainSide.marks` is keyed by Decimal (see the identical wrap at
        # every other call site in this file). A string key here silently
        # missed every mark -> permanent quote-guard deferral -> the catch-up
        # never actually recovered a long, with every wiring test green
        # (2026-07-10 review finding; pinned by
        # test_stop_fill_detector_drives_lex_with_a_real_quote... in
        # tests/application/test_live_app.py).
        strike = Decimal(_strike_from_symbol(long_symbol))
        side_chain = snap.put_side if side == "PUT" else snap.call_side
        mark = side_chain.marks.get(strike)
        spot = getattr(snap, "spot", None)
        if spot is None:
            return None  # EC-LEX-08(c): no underlying mark at all -- cannot price a floor either
        if mark is None:
            # EC-LEX-08(a)/(c): the strike itself carries no bid at all.
            if snap.stale:
                # A stale spot is not DAT-02-fresh -- never floor off stale
                # data; defer honestly (case (c) territory until it refreshes).
                return None
            intrinsic = intrinsic_put(strike, spot) if side == "PUT" else intrinsic_call(strike, spot)
            return NoBidFloor(intrinsic=intrinsic)
        intrinsic = intrinsic_put(strike, spot) if side == "PUT" else intrinsic_call(strike, spot)
        from meic.application.recover_long import Quote

        quote = Quote(bid=mark.bid, ask=mark.ask)
        if snap.stale:
            return StaleQuote(quote=quote, intrinsic=intrinsic)
        return quote, intrinsic

    async def _detect_stop_fills() -> None:
        """EC-STP-06 (v1.60): catch up any stop fill this process missed
        while it was UP and running — the exact gap behind the 2026-07-10
        11:56 incident (the C7565 CALL stop filled at 11:56:15 ET and nothing
        noticed: no SIDE_STOPPED, no LEX, no UI feedback). Run every health
        tick (see `_probe_once`); `comp.alerts` is read at CALL time (not
        closure-construction time) so it resolves to whichever AlertSink
        `live_app()` ends up assigning."""
        from meic.application.stop_fill_watch import detect_and_recover_stop_fills

        await detect_and_recover_stop_fills(comp, comp.alerts, _long_quote)

    def _floor_candidates(row) -> dict:
        """ENT-09b v1.57: the ▶ dialog's floor dropdowns. Thin -- the actual
        computation is the pure, independently-tested
        `composition.live_selection.floor_candidates`; this closure only
        supplies the live snapshot and the row's own SelectionConfig."""
        cfg = SelectionConfig.for_entry(row) if row is not None else selector.config
        return floor_candidates_fn(snaps.last, cfg)

    return {
        "runtime": runtime,
        "manual": manual,
        # ENT-09b v1.57: the ▶ dialog's floor-dropdown data provider.
        "floor_candidates": _floor_candidates,
        # UC-02: real checks. `data_fresh` is read synchronously off the cached
        # snapshot — the pre-flight route runs on a threadpool and must not await
        # the broker (that would bind its session to a fresh event loop).
        "preflight_checks": live_preflight_checks(
            comp, data_fresh=lambda: not snaps.stale,
            drift=drift, max_drift_ms=max_drift_ms),
        # the ~60s session probe, which also records the DAY-03 clock reading. The
        # health loop runs it live; exposed so the wiring test can drive one tick.
        "session_probe": _session_valid,
        # DAT-02: refresh the chain snapshot so `data_fresh` (and the UC-02
        # market_data pre-flight) reflect live data. The health loop runs it; the
        # selector also takes its own snapshot at fire time.
        "data_probe": snaps.take,
        # FEATURE 3: the holder itself, so live_app can build the live-P/L
        # entries_enricher off `.last`/`.stale` — no new subscription.
        "snapshots": snaps,
        # TPF-03/TPT-04: the bot-side profit monitor, evaluated each health
        # tick (see `_probe_once`) and once more, immediately, on recovery
        # (`_recover_exits_once`, TPF-08/TPT-07).
        "exit_monitor": exit_monitor,
        # EC-STP-06 (v1.60): the live stop-fill catch-up pass, run every
        # health tick — the fourth "exists but unwired" member (after RSK-04,
        # the day supervisor, and TPF/TPT): exposed on app.state so the rail
        # capstone (tests/application/test_live_app.py) can assert it is a
        # REAL callable, not None.
        "stop_fill_detector": _detect_stop_fills,
        # NFR-04 (2026-07-13): the QuoteHub the live quote-stream loop writes
        # and the enrichers/evaluator read live-first, snapshot-fallback.
        "quote_hub": quote_hub,
        "max_quote_age_ms": max_quote_age_ms,
    }


def _max_entries(comp) -> int | None:
    """ENT-05. `None` means 'as many as are composed' (doc 06 default)."""
    schedule = comp.state.entry_schedule or []
    return len(schedule) or None


def live_app():
    """Live composition behind the panel: real broker + feed, SQLite-persisted,
    token-gated, safe defaults. Connects on startup; NO trading auto-starts —
    the operator arms + confirms live deliberately. CERT sandbox by default."""
    from meic.adapters.api.app import create_app
    from meic.adapters.persistence.event_store import SqliteStateStore
    from meic.application.clocks import SystemClock
    from meic.application.watchdog import StopWatchdog
    from meic.composition.live import LiveComposition
    from meic.composition.panel_commands import PanelCommands

    env = _read_env()
    is_test = env.get("MEIC_LIVE_IS_TEST", "true").lower() != "false"
    token = env.get("MEIC_USER_PASSWORD")
    if not token:
        raise RuntimeError("live panel requires MEIC_USER_PASSWORD (NFR-06) — set it in .env/env")

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
    # The mode pill reflects the process: this one is bound to the REAL broker,
    # so the UI shows LIVE (and the Confirm-Live modal shows the real-money warning).
    comp.state.trading_mode = "live"

    # The live day, assembled with EVERY safety rail armed. Built BEFORE create_app
    # so the panel's ▶ button and pre-flight get the real thing, not stubs. See
    # composition/live_wiring.py — and tests/composition/test_live_wiring.py, which
    # asserts on those functions precisely because this is where rails go missing.
    live = _wire_live_day(comp, env)

    def _drill_guidance_provider() -> list[str]:
        """UC-12 v1.56: advisory-only warnings for the drill confirmation
        dialog. `entry_soon` is real (the composed schedule's own next-fire
        time). `near_trigger` (operator ruling 2026-07-11): real too, off the
        SAME open-short frame EC-STP-06's catch-up uses
        (`stop_fill_watch._open_short_legs` -- an open short with a
        `StopPlaced` on record) and the live chain snapshot FEATURE 3 already
        holds -- no new subscription. Each short's fill/trigger feed the
        SAME shared formula RPT-12's MAE uses
        (`reporting.mae_mfe.consumed_fraction` via
        `application.drills.near_trigger_status`); a mark this tick cannot
        price (no snapshot, stale, or the strike unmarked) is `None` --
        honest 'unknown', never a guess and never silently treated as
        'not near'."""
        from datetime import timedelta as _td

        from meic.application.drills import OpenShortMark, drill_guidance, near_trigger_status
        from meic.application.stop_fill_watch import _open_short_legs
        from meic.composition.live_gates import ET as _ET
        from meic.composition.live_wiring import schedule_rows

        now = comp.clock.now()
        # DAY-03: `comp.clock.now()` is UTC-aware (SystemClock) -- the ET trading
        # day, never its own `.date()`, is what `today`/the day-scope string must
        # be (previously agreed only by luck: this only ever runs inside market
        # hours, when the UTC and ET calendar dates happen to coincide).
        today = trading_day(now)
        rows = schedule_rows(comp.state, today=today, tz=_ET)
        remaining = _remaining_rows(rows, now, comp.events, today.isoformat())
        entry_soon = bool(remaining) and (min(r.when for r in remaining) - now) <= _td(seconds=600)

        snap = live["snapshots"].last
        shorts: list[OpenShortMark] = []
        for _entry_id, side, leg, spec in _open_short_legs(comp.events):
            mark = None
            if snap is not None and not snap.stale:
                side_chain = snap.put_side if side == "PUT" else snap.call_side
                mark = _leg_mid(side_chain, Decimal(_strike_from_symbol(leg.symbol)))
            shorts.append(OpenShortMark(fill=leg.price, trigger=spec.trigger, mark=mark))

        return drill_guidance(near_trigger=near_trigger_status(shorts), entry_soon=entry_soon)

    # NFR-04 (2026-07-13): the QuoteHub -- see `_wire_live_day` -- and its
    # freshness bar, threaded into every consumer below so live P/L, TPF/TPT
    # and the panel's own gap-validation provider all resolve marks off the
    # SAME live-first/snapshot-fallback rule (`_resolve_leg_mid`).
    hub = live["quote_hub"]
    max_quote_age_ms = live["max_quote_age_ms"]

    commands = PanelCommands(comp, manual_entry=live["manual"],
                             preflight_checks=live["preflight_checks"],
                             # TPF-02/TPT-03: server-side gap validation off the
                             # SAME evaluator/snapshot the health-tick monitor uses.
                             profit_pct_provider=_profit_pct_provider(
                                 comp, live["snapshots"], hub, clock=comp.clock,
                                 max_quote_age_ms=max_quote_age_ms),
                             # ENT-09b v1.57: the ▶ dialog's floor dropdowns.
                             floor_candidates_provider=live["floor_candidates"],
                             # UC-12 v1.56: the outage-drill dialog's advisory warnings.
                             drill_guidance_provider=_drill_guidance_provider,
                             default_drill_outage_seconds=_drill_outage_seconds(env))
    # FEATURE 3 + UI-13/14/15: live P/L, then the shared TPF/TPT profit%, both
    # off the already-held chain snapshot — no new subscription — PLUS the
    # NFR-04 QuoteHub for a live-first mark per leg (falls back to the
    # snapshot exactly as before when the hub has nothing fresh). paper_app
    # passes no enricher at all (SIM-01 marks are synthetic, nothing honest to
    # show for either).
    live_pnl_enricher = _live_pnl_enricher(comp, live["snapshots"], hub, clock=comp.clock,
                                           max_quote_age_ms=max_quote_age_ms)
    profit_pct_enricher = _profit_pct_enricher(comp, live["snapshots"], hub, clock=comp.clock,
                                               max_quote_age_ms=max_quote_age_ms)

    def entries_enricher(cards: list[dict]) -> list[dict]:
        return profit_pct_enricher(live_pnl_enricher(cards))

    reporting_config = _reporting_config(
        env, stop_loss_pct=lambda: _current_stop_loss_pct(comp.state))
    # RPT-16: the SAME read-only facade RPT-15's reconciler uses (day_fills +
    # day_settlements only) -- never comp.broker directly -- so the one-time
    # backfill endpoint is structurally incapable of any order action either.
    app = create_app(comp.state, comp.events, api_token=token, commands=commands,
                     entries_enricher=entries_enricher,
                     reporting_config=reporting_config,
                     backfill_broker_reads=_BrokerReadFacade(comp.broker))
    app.state.composition = comp
    app.state.commands = commands
    app.state.session_probe = live["session_probe"]   # DAY-03 clock reading source
    app.state.exit_monitor = live["exit_monitor"]     # TPF-03/TPT-04 bot-side monitor
    app.state.stop_fill_detector = live["stop_fill_detector"]  # EC-STP-06 catch-up (v1.60)
    # ITEM 1 (operator ruling 2026-07-11): shared lock between the two
    # stop-fill-detector callers -- the order-event push consumer (BLOCKS via
    # run_pass_locked so a fill event mid-pass is never dropped) and the
    # dedicated fallback poll loop below (SKIPS via run_pass_if_idle instead
    # of queuing) -- so the pass itself is always single-flighted no matter
    # which caller reaches it. See application/order_event_watch.py for the
    # asymmetry between the two helpers.
    app.state.stop_fill_lock = asyncio.Lock()
    # The held chain-snapshot holder itself (same object every enricher/monitor
    # reads) — exposed like exit_monitor above so the EC-STP-06 end-to-end test
    # can install a snapshot and prove `_long_quote` actually reads marks off
    # it (the wiring capstone's non-None check alone cannot catch a detector
    # that is wired but reads nothing — the 2026-07-10 review finding).
    app.state.chain_snapshots = live["snapshots"]
    # NFR-04 (2026-07-13): the QuoteHub itself, exposed like `chain_snapshots`
    # above so a test/operator can inspect `.healthy`/`.mark(symbol)` directly.
    app.state.quote_hub = hub
    app.state.broker_connected = False
    app.state.broker_error = None
    app.state.reconcile = None
    alerts = _PanelAlerts()
    app.state.alerts = alerts
    comp.alerts = alerts  # critical alerts must reach the operator, not /dev/null

    # RPT-15: the EOD broker reconcile-and-correct reconciler. `_BrokerReadFacade`
    # is the ONLY thing it is ever handed -- never `comp.broker` directly --
    # so it is structurally incapable of any order action.
    from meic.application.report_reconciler import ReportReconciler

    report_reconciler = ReportReconciler(broker=_BrokerReadFacade(comp.broker),
                                         events=comp.events, alerts=alerts)
    app.state.report_reconciler = report_reconciler  # exposed for tests/ops visibility
    # EOD-01 v1.59: a second `_BrokerReadFacade` instance (same read-only
    # shape as the reconciler's) for settlement capture, run BEFORE the
    # reconcile compare in `_probe_once` below.
    settlement_broker_reads = _BrokerReadFacade(comp.broker)
    settlement_lookback_days = _settlement_lookback_days(env)
    # EOD-03 (2026-07-11): the sweep's half-day calendar — the SAME algorithmic
    # exchange facts the DAY-01/02 gates use (nyse_holidays), a decade out, so
    # a 13:00 half-day close sweeps at 13:00 (DAY-02), never a hardcoded 16:00.
    # DAY-03: anchored on the ET trading day, not a UTC boot-time date (same
    # New Year's Eve boundary concern as `_cal_anchor` above).
    eod_half_days = half_days_near(trading_day(comp.clock.now()), years_ahead=10)

    async def _boot_reconcile() -> None:
        """REC-02/04: adopt broker truth before any trading is possible. Anything
        the bot's durable ledger cannot account for is FOREIGN -> quarantined and
        entries stay blocked until the operator resolves it."""
        from meic.application.reconcile_boot import reconcile_on_boot
        from meic.application.stop_fill_watch import readopt_resting_floors

        result = await reconcile_on_boot(
            broker=comp.broker, events=comp.events, state=comp.state, alerts=alerts)
        app.state.reconcile = result
        # EC-LEX-08(d) (v1.64): the in-memory floor registry does not survive
        # a restart -- re-adopt any still-resting intrinsic-floor sell before
        # the stop-fill poll loop (or the order-event push consumer) can run
        # its first pass, so supersession/fill-recognition resumes exactly as
        # it does for a resumed ladder (REC-03).
        await readopt_resting_floors(comp, comp.broker)

    async def _probe_once() -> None:
        """One health tick: the NFR-02 session probe (which records the DAY-03
        broker-clock reading off the response's Date header) and a DAT-02 chain
        snapshot refresh (so `market_data` reflects live data). Each is best-effort
        and independent — a failure in one is surfaced but never blocks the other or
        crashes the loop; the next tick retries."""
        try:
            await live["session_probe"]()
        except Exception as exc:  # noqa: BLE001
            app.state.broker_error = repr(exc)
        try:
            await live["data_probe"]()
        except Exception as exc:  # noqa: BLE001
            app.state.broker_error = repr(exc)
        try:
            # RPT-12/D8: sample marks off the snapshot just refreshed above,
            # same cadence, independent of either probe's success (the
            # sampler itself degrades to a no-op on a missing/stale snapshot).
            _sample_marks_once(comp, live["snapshots"].last)
        except Exception as exc:  # noqa: BLE001
            app.state.broker_error = repr(exc)
        try:
            # TPF-03/TPT-04: the bot-side profit monitor, same cadence as the
            # marks sample above (same snapshot, no new subscription). NFR-04:
            # `hub` lets the evaluator prefer a live mark per leg.
            await _evaluate_exits_once(comp, live["snapshots"].last,
                                       live["exit_monitor"], commands,
                                       hub=hub, clock=comp.clock, max_quote_age_ms=max_quote_age_ms)
        except Exception as exc:  # noqa: BLE001
            app.state.broker_error = repr(exc)
        # EC-STP-06 (v1.60) stop-fill catch-up MOVED OFF this tick (operator
        # ruling 2026-07-11, ITEM 1's follow-up): it used to run here, inline,
        # every ~60s. It now runs on its OWN dedicated poll loop
        # (`_start_stop_fill_poll_loop` below, `MEIC_STOP_FILL_POLL_S`,
        # default 15s), skip-if-busy against `stop_fill_lock` -- one owner
        # per concern, and a shorter, independently-tunable cadence than the
        # rest of this tick's duties need. See order_event_watch.py for the
        # two callers (this loop and the order-event push consumer) that now
        # share that lock.
        try:
            # EOD-03 (2026-07-11 wiring): the day-end order-audit sweep —
            # at/after the CALENDAR session close (13:00 on half days), once
            # per day, journal-gated. Runs BEFORE the settlement/reconcile
            # region below: cancel-and-confirm the working orders first, then
            # count the money. A crash here retries next tick, never a crash.
            await _maybe_eod_sweep_once(comp, lambda: datetime.now(ET),
                                        half_days=eod_half_days)
        except Exception as exc:  # noqa: BLE001
            app.state.broker_error = repr(exc)
        try:
            # RPT-15: once per tick, past EOD settlement, on a day with
            # activity, not yet reconciled -- see _maybe_eod_reconcile_once's
            # own idempotency rule. A broker-unreachable outcome here is
            # NOT an error (RPT-15: retries next tick/boot, never a crash).
            await _maybe_eod_reconcile_once(app.state, comp, report_reconciler,
                                            lambda: datetime.now(ET),
                                            broker_reads=settlement_broker_reads,
                                            lookback_days=settlement_lookback_days)
        except Exception as exc:  # noqa: BLE001
            app.state.broker_error = repr(exc)

    @app.on_event("startup")
    async def _connect() -> None:
        # A broker/network hiccup must NOT take down the operator's control
        # panel: come up regardless, record the status, let it be retried.
        try:
            await comp.connect(account)
            app.state.broker_connected = True
            await _boot_reconcile()
            # DAY-03: take one clock reading immediately so the operator can arm
            # without waiting a whole health-loop interval for the first probe.
            await _probe_once()
            # TPF-08/TPT-07: an already-breached floor/reached target fires
            # IMMEDIATELY on recovery — after boot reconcile (so a synthesized
            # stop event has already disarmed any TPT-05 target) and after the
            # probe above (so a fresh snapshot exists to mark against). NFR-04:
            # same hub-aware resolution as the health tick above.
            await _recover_exits_once(comp, live["snapshots"].last, commands,
                                      hub=hub, clock=comp.clock, max_quote_age_ms=max_quote_age_ms)
        except Exception as exc:  # noqa: BLE001 — surfaced, never fatal
            app.state.broker_error = repr(exc)

    # NFR-02 + DAY-03: the periodic health loop the gates and pre-flight assume
    # exists. It keeps the session-liveness and broker-clock reading FRESH (a
    # reading older than 300 s is treated as unverified). Without it the clock is
    # never verified and the arm pre-flight blocks forever. Runs on the main event
    # loop — the SAME loop comp.connect bound the broker session to — so awaiting
    # broker calls here is safe (unlike the threadpool pre-flight route).
    health_interval_s = float(env.get("MEIC_HEALTH_INTERVAL_S", "60"))

    @app.on_event("startup")
    async def _start_health_loop() -> None:
        async def _loop() -> None:
            while True:
                await asyncio.sleep(health_interval_s)
                if app.state.broker_connected:
                    await _probe_once()
        app.state.health_task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_health_loop() -> None:
        task = getattr(app.state, "health_task", None)
        if task:
            task.cancel()

    # ITEM 1 (operator ruling 2026-07-11): "the stop being hit triggers the
    # long sale immediately; only if that fails does the periodic check force
    # it." Alongside the health loop above -- created unconditionally at
    # startup, same shape (one supervised background task, cancelled on
    # shutdown). It does not wait for `_connect()` to succeed first: reusing
    # the adapter's own `order_events()` on a session that is not yet
    # connected simply fails like any other stream death, and
    # `consume_order_events`'s own reconnect/backoff loop retries until
    # `comp.connect` (a separate startup hook) has made the session live --
    # so this functionally "starts on broker connect" without the two hooks
    # needing to coordinate directly. Reuses `live["stop_fill_detector"]`,
    # the SAME closure `_probe_once` calls -- one decision path, matched
    # single-flight via `stop_fill_lock` (see order_event_watch.py).
    @app.on_event("startup")
    async def _start_order_event_consumer() -> None:
        from meic.application.order_event_watch import consume_order_events

        app.state.order_event_task = asyncio.create_task(
            consume_order_events(comp.broker.order_events, live["stop_fill_detector"],
                                 app.state.stop_fill_lock, alerts))

    @app.on_event("shutdown")
    async def _stop_order_event_consumer() -> None:
        task = getattr(app.state, "order_event_task", None)
        if task:
            task.cancel()

    # ITEM 1 follow-up (operator ruling 2026-07-11): the stop-fill FALLBACK
    # poll gets its OWN dedicated loop -- previously it rode the ~60s health
    # loop above (see `_probe_once`, which no longer drives this pass: one
    # owner per concern). Same shape as the health loop and the order-event
    # consumer above (one supervised background task, created unconditionally
    # at startup, cancelled on shutdown). Skip-if-busy against the SAME
    # `stop_fill_lock` the push consumer uses: if a push-triggered pass or a
    # still-running LEX ladder already holds the lock, this tick is SKIPPED
    # outright -- it never queues behind the lock (`run_pass_if_idle`,
    # deliberately asymmetric against the push path's own blocking
    # `run_pass_locked` -- see order_event_watch.py for why: a fill event
    # landing mid-pass must never be dropped, but a fallback tick with
    # nothing specific to react to has nothing to gain by waiting). The pass
    # itself (`detect_and_recover_stop_fills`) is journal-terminal-aware -- a
    # side already sold/closed on the durable event log is never re-tried
    # (pinned in tests/application/test_stop_fill_watch.py) -- so this
    # fallback only ever steps in for work the push path has not already
    # completed; a skipped or a spurious extra tick is equally harmless.
    stop_fill_poll_interval_s = _stop_fill_poll_seconds(env)
    # exposed for the wiring capstone (tests/application/test_live_app.py) --
    # proves the loop's cadence actually comes from env, not a hardcoded value.
    app.state.stop_fill_poll_interval_s = stop_fill_poll_interval_s

    @app.on_event("startup")
    async def _start_stop_fill_poll_loop() -> None:
        from meic.application.order_event_watch import run_pass_if_idle

        async def _loop() -> None:
            while True:
                await asyncio.sleep(stop_fill_poll_interval_s)
                if app.state.broker_connected:
                    try:
                        await run_pass_if_idle(app.state.stop_fill_lock, live["stop_fill_detector"])
                    except Exception as exc:  # noqa: BLE001 -- must never crash the app
                        app.state.broker_error = repr(exc)
        app.state.stop_fill_poll_task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_stop_fill_poll_loop() -> None:
        task = getattr(app.state, "stop_fill_poll_task", None)
        if task:
            task.cancel()

    # NFR-04 (2026-07-13): the live quote-stream loop -- same shape as the
    # health loop and the stop-fill poll loop above (one supervised background
    # task, created unconditionally at startup, cancelled on shutdown). Keeps
    # `hub` ticking off the CURRENT open entries' leg symbols so
    # `_live_pnl_enricher`/`_entry_profit_pct_now` can read a live mark instead
    # of the chain snapshot's ~60s cadence; falls back to that exact snapshot
    # path whenever the hub has nothing fresh (`_resolve_leg_mid`), so a
    # disconnected/sick/never-started stream is byte-identical to before this
    # wiring existed.
    quote_stream_poll_s = _quote_stream_poll_seconds(env)
    app.state.quote_stream_poll_s = quote_stream_poll_s

    @app.on_event("startup")
    async def _start_quote_stream_loop() -> None:
        # `live["snapshots"]` is passed because the OCC->STREAMER translation
        # (`_streamer_symbol`) reads its strike->streamer map: DXLink only
        # speaks the streamer namespace. Until the first snapshot lands there is
        # nothing subscribable, and the loop simply idles.
        app.state.quote_stream_task = asyncio.create_task(_run_quote_stream_loop(
            comp, hub, comp.feed, live["snapshots"], alerts,
            idle_seconds=quote_stream_poll_s, retry_seconds=quote_stream_poll_s,
            connected=lambda: app.state.broker_connected))

    @app.on_event("shutdown")
    async def _stop_quote_stream_loop() -> None:
        task = getattr(app.state, "quote_stream_task", None)
        if task:
            task.cancel()

    # STP-03b (2026-07-13): the stop watchdog -- a SECOND, bot-side trigger
    # layer over the resting broker stop, which stays PRIMARY and bot-
    # independent (the tastytrade adapter's own trigger-source verdict is
    # indeterminate -- adapters/tastytrade/adapter.py's own docstring line 8).
    # `StopWatchdog` (application/watchdog.py) was fully written and unit-
    # tested but never constructed, ticked, or wired into the live app --
    # grep confirmed the only references anywhere were a health-panel counter
    # and an activity-feed icon. Fed the SAME live QuoteHub the quote-stream
    # loop above keeps ticking, translated through the SAME streamer-symbol
    # seam the P&L path uses (`_resolve_leg_mid`) -- one mark source, no
    # drift. Same shape as every other supervised background task here (one
    # task, created unconditionally at startup, cancelled on shutdown); polls
    # on the SAME cadence as the quote-stream loop it reads from -- ticking
    # faster than the hub itself refreshes would gain nothing, so this reuses
    # `quote_stream_poll_s` rather than inventing a new infra dial.
    watchdog_grace_s = _watchdog_grace_seconds(env)
    watchdog_escalate_s = _watchdog_escalate_seconds(env)
    app.state.watchdog_grace_seconds = watchdog_grace_s
    app.state.watchdog_escalate_seconds = watchdog_escalate_s
    stop_watchdog = StopWatchdog(broker=comp.broker, alerts=alerts, events=comp.events,
                                 grace_seconds=watchdog_grace_s, escalate_seconds=watchdog_escalate_s,
                                 fee_model=comp.fee_model)
    app.state.stop_watchdog = stop_watchdog

    @app.on_event("startup")
    async def _start_stop_watchdog_loop() -> None:
        app.state.stop_watchdog_task = asyncio.create_task(_run_stop_watchdog_loop(
            comp, stop_watchdog, hub, live["snapshots"], alerts, clock=comp.clock,
            max_quote_age_ms=max_quote_age_ms, idle_seconds=quote_stream_poll_s,
            connected=lambda: app.state.broker_connected))

    @app.on_event("shutdown")
    async def _stop_stop_watchdog_loop() -> None:
        task = getattr(app.state, "stop_watchdog_task", None)
        if task:
            task.cancel()

    @app.post("/broker/connect")
    async def broker_connect() -> dict:
        """Retry the broker session + boot reconcile (token-gated by middleware)."""
        try:
            await comp.connect(account)
            app.state.broker_connected = True
            app.state.broker_error = None
            await _boot_reconcile()
            await _probe_once()   # DAY-03: verify the clock on reconnect too
            # TPF-08/TPT-07, NFR-04: same hub-aware resolution as the health tick.
            await _recover_exits_once(comp, live["snapshots"].last, commands,
                                      hub=hub, clock=comp.clock, max_quote_age_ms=max_quote_age_ms)
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

    @app.post("/reports/reconcile/{day}")
    async def reconcile_day_on_demand(day: str) -> dict:
        """PNL-04: "At EOD (**and on demand**)" -- an operator-triggered
        reconcile for `day`, run right now, using the SAME `report_reconciler`
        instance (and its `_BrokerReadFacade`) the EOD health tick calls via
        `_maybe_eod_reconcile_once` above -- never a second, separately-wired
        reconciler.

        Deliberately does NOT consult that function's already-resolved gate:
        an explicit operator request must always run, even on a day the
        automatic tick would skip -- including a day whose only prior record
        is a pre-fix LEGACY `CorrectionRecord` (`scope != "own"`, see that
        gate's docstring above), which is exactly the case an operator would
        reach for this endpoint to fix. A broker-unreachable outcome is
        surfaced as-is (`ReconcileOutcome.status == "unreachable"`), never
        caught-and-swallowed.

        Mutating POST -> gated by the SAME auth/origin security middleware as
        every other command (adapters/api/app.py's `security` middleware:
        NFR-06 origin check + `x-api-token`)."""
        import re

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise HTTPException(status_code=422, detail="bad_day_format")
        outcome = await report_reconciler.reconcile_day(day)
        return {
            "day": outcome.day,
            "status": outcome.status,
            "corrections": [
                {"field": c.field, "bot_value": c.bot_value,
                 "broker_value": c.broker_value, "diff": c.diff}
                for c in outcome.corrections
            ],
            "ambiguous_settlements": outcome.ambiguous_settlements,
        }

    @app.get("/alerts")
    def recent_alerts() -> list[dict]:
        return alerts.recent()

    # --- live trading day: the runtime was assembled above (see _wire_live_day) --
    from meic.composition.live_gates import ET

    runtime = live["runtime"]
    app.state.runtime = runtime
    app.state.day_task = None

    def _todays_entry_times():
        """Today's ScheduledRows — each carrying its OWN ENT-04 settings.

        This used to return bare datetimes: the composed rows' contracts, premium,
        width and stop were parsed off and thrown away, so every live entry traded
        1 contract at the globals no matter what the panel displayed.
        """
        from meic.composition.live_wiring import schedule_rows
        return schedule_rows(comp.state, today=datetime.now(ET).date(), tz=ET)

    # exposed so the live-wiring capstone can assert on the REAL row construction
    app.state.todays_rows = _todays_entry_times

    @app.post("/day/start")
    async def day_start() -> dict:
        """Start the wall-clock trading day, manually.

        ENT-10: one code path, one set of guarantees for what run_day is given —
        this endpoint hands run_day exactly what the supervisor would: the
        REMAINING, originally-numbered rows, and only while ARMED. A disarmed
        start used to walk every row and persist EntrySkipped(DISARMED) for the
        whole schedule, which the remaining-rows filter then read as "already
        attempted" — silently disabling the entire day even after a real arm.
        """
        task = app.state.day_task
        if task is not None and not task.done():
            return {"running": True, "already_running": True}
        if not comp.state.armed:
            raise HTTPException(status_code=400, detail="not_armed")
        times = _todays_entry_times()
        if not times:
            raise HTTPException(status_code=400, detail="no_entries_composed")
        now = datetime.now(ET)
        day = now.date().isoformat()
        rows = _remaining_rows(times, now, comp.events, day)
        if not rows:
            return {"running": False, "reason": "no_remaining_entries"}
        app.state.day_task = asyncio.create_task(runtime.run_day(day, rows))
        return {"running": True, "day": day, "entries": len(rows)}

    @app.post("/day/stop")
    async def day_stop() -> dict:
        task = app.state.day_task
        if task is not None and not task.done():
            task.cancel()
        return {"running": False}

    @app.get("/day/status")
    def day_status() -> dict:
        task = app.state.day_task
        # UI-24 + ENT-10: the operator-visible watch state, always present
        # regardless of whether a day task has ever run. Computed over the SAME
        # filtered set the supervisor hands run_day — an entry already attempted
        # today (e.g. fired early via ENT-09) must not show as "next".
        now = datetime.now(ET)
        if is_trading_day(now.date(), holidays=holidays_near(now.date())):
            remaining = _remaining_rows(_todays_entry_times(), now, comp.events,
                                        now.date().isoformat())
            extras = _day_status_extras(remaining, now)
        else:
            # DAY-01/UI-24 (operator ruling 2026-07-11): weekends and market
            # holidays roll the countdown to the next trading day's first entry.
            extras = _next_trading_day_extras(comp.state, now)
        base = {"armed": comp.state.armed,
                # RSK-06: a supervisor whose ticks are failing must say so —
                # None when healthy, the last failure's repr otherwise.
                "supervisor_error": getattr(app.state, "day_supervisor_error", None),
                **extras}
        if task is None:
            return {**base, "started": False, "running": False}
        if not task.done():
            return {**base, "started": True, "running": True}
        if task.cancelled():
            return {**base, "started": True, "running": False, "cancelled": True}
        exc = task.exception()
        return {**base, "started": True, "running": False,
                "filled": None if exc else task.result(),
                "error": repr(exc) if exc else None}

    @app.get("/calendar/adjacent-trading-day")
    def adjacent_trading_day(
        from_: str = Query(..., alias="from"),
        dir: str = Query(...),
    ) -> dict:
        """DAY-01: step the Results day picker to the previous/next NYSE session,
        skipping weekends AND market holidays. Read-only calendar math over the SAME
        exchange calendar the countdown uses; never a trading input (UI-03). `next`
        never returns a date past today's ET session (no navigating into the future)."""
        try:
            d = date.fromisoformat(from_)
        except ValueError:
            raise HTTPException(status_code=422, detail="from must be YYYY-MM-DD")
        if dir not in ("prev", "next"):
            raise HTTPException(status_code=422, detail="dir must be 'prev' or 'next'")
        # 3-year window so a single ±1 session step is correct across a year boundary.
        holidays = nyse_holidays(d.year - 1) | nyse_holidays(d.year) | nyse_holidays(d.year + 1)
        if dir == "prev":
            c = d - timedelta(days=1)
            while not is_trading_day(c, holidays=holidays):
                c -= timedelta(days=1)
            return {"date": c.isoformat()}
        nxt = next_trading_day(d, holidays=holidays)  # strictly after d
        today = datetime.now(ET).date()
        return {"date": nxt.isoformat() if nxt <= today else None}

    # ENT-10: arming runs the day. This supervisor is what turns "ARMED" from a
    # state flag into a running watch — it starts run_day for the REMAINING
    # schedule on arm (and on boot-restore, since it is the SAME loop either
    # way), cancels on disarm, and alerts-once (never auto-retries) on a crash.
    supervisor_interval = float(env.get("MEIC_DAY_SUPERVISOR_INTERVAL_S", "2.0"))
    app.state.day_task_failed = False
    app.state.day_supervisor_error = None   # last tick failure, repr — None when healthy

    @app.on_event("startup")
    async def _start_day_supervisor() -> None:
        async def _loop() -> None:
            while True:
                await _supervisor_tick(app.state, comp, alerts, _todays_entry_times,
                                       runtime, lambda: datetime.now(ET))
                await asyncio.sleep(supervisor_interval)
        app.state.day_supervisor = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_day_supervisor() -> None:
        task = getattr(app.state, "day_supervisor", None)
        if task:
            task.cancel()
        day_task = getattr(app.state, "day_task", None)
        if day_task:
            day_task.cancel()

    _serve_panel(app)
    return app
