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
    from decimal import Decimal

    from meic.adapters.api.app import create_app
    from meic.application.entry_gates import GateSnapshot, RiskSnapshot
    from meic.application.manual_entry import ManualEntry
    from meic.composition.panel_commands import PanelCommands
    from meic.domain.projection import fold

    comp = PaperComposition(clock=MutableClock(datetime(2026, 7, 7, 9, 30, tzinfo=timezone.utc)), ticks=SPX)
    comp.state.trading_mode = "paper"   # honest: this process holds the simulator
    runtime = PaperDemoRuntime(comp, step_seconds=3.0)

    async def selector(when, n, config=None):
        """The demo's stand-in for live selection. A real day selects from the
        DXLink chain; the shape — and the row's contracts — is identical."""
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
    app = create_app(comp.state, comp.events, commands=PanelCommands(comp, manual_entry=manual))

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


def _chain_band_pts(env: dict[str, str]) -> Decimal:
    """STK-10 `chain_atm_band_pts` (doc 06: range 50-500, default 150) — how far from
    spot the completeness gate inspects. Read from env (like the other live tunables);
    an out-of-range value falls back to the spec default rather than trading a silly
    band. Far-OTM 0DTE strikes are listed but usually unquoted, so this is the knob
    that keeps STK-10 from failing on strikes that will never have a mark."""
    try:
        raw = Decimal(env.get("MEIC_CHAIN_ATM_BAND_PTS", "150"))
    except (ArithmeticError, ValueError):
        return Decimal("150")
    return raw if Decimal("50") <= raw <= Decimal("500") else Decimal("150")


def _chain_completeness_pct(env: dict[str, str]) -> Decimal:
    """STK-10 `chain_completeness_pct` (doc 06: range 50-100, default 90) — the % of
    ATM-band strikes that must carry marks before selection. Doc-06-defined but never
    wired (the selector hardcoded 90). The far-OTM 0DTE quote boundary RECEDES toward
    the money late in the day, so with the band already at its 50-pt floor this is the
    remaining spec dial that keeps dead listed strikes from vetoing selection.
    Out-of-range falls back to the spec default (reject-the-dial, trade the default)."""
    try:
        raw = Decimal(env.get("MEIC_CHAIN_COMPLETENESS_PCT", "90"))
    except (ArithmeticError, ValueError):
        return Decimal("90")
    return raw if Decimal("50") <= raw <= Decimal("100") else Decimal("90")


def _remaining_rows(rows, now, events, day):
    """ENT-10: the rows a day task started NOW should attempt — future-timed
    (row.when > now) and not already attempted today (no CondorFilled with
    entry_id == f"{day}#{n}" and no EntrySkipped with date==day and
    entry_number==n in events). Rows keep their original numbers.
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
        "seconds_to_next": int((nxt.when - now).total_seconds()),
        "entries_remaining": len(remaining),
    }


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
    rows = _remaining_rows(todays_rows(), now, comp.events, now.date().isoformat())
    if rows:
        app_state.day_task = asyncio.create_task(runtime.run_day(now.date().isoformat(), rows))


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


def _wire_live_day(comp, env: dict[str, str]) -> dict:
    """Assemble the live trading day: selector, gates, runtime, ▶, pre-flight.

    Thin: every decision that could leave a SAFETY RAIL unarmed lives in
    composition/live_wiring.py, where tests/composition/test_live_wiring.py asserts
    on it directly. That test exists because this function's predecessor built a
    LiveRuntime with max_day_risk, order_cap and buying_power all left at None,
    and threw the composed schedule rows away — while the paper composition and
    every unit test had all of it armed.
    """
    from meic.composition.live_gates import LiveMarketGates
    from meic.composition.live_selection import LiveCondorSelector, SelectionConfig
    from meic.composition.live_wiring import (
        BrokerClockProbe,
        build_live_runtime,
        build_manual_entry,
        live_preflight_checks,
    )

    min_buying_power = Decimal(env.get("MEIC_MIN_BUYING_POWER", "5000"))
    max_drift_ms = float(env.get("MEIC_MAX_CLOCK_DRIFT_MS", "2000"))   # DAY-03 v1.48

    chain_band = _chain_band_pts(env)   # STK-10 chain_atm_band_pts, now actually wired

    # DAY-03 (v1.48): drift is measured against the BROKER's Date header on the
    # ~60 s session probe — no env var, no NTP. Starts unverified (infinite drift),
    # so entries are blocked until the first probe lands; a reading older than 300 s
    # is treated as unverified too. The session probe below feeds it.
    drift = BrokerClockProbe()

    class _Snapshots:
        """Freshness of the most recent chain snapshot, so the DAT-02 gate — and
        the UC-02 pre-flight — reflect the data the selector actually used.
        Starts STALE: unknown freshness is never 'fresh'."""
        stale = True

        async def take(self):
            from meic.adapters.dxlink.chain_snapshot import snapshot_chain
            snap = await snapshot_chain(comp.broker._session, band_points=chain_band)
            self.stale = snap.stale
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
        config=SelectionConfig(completeness_pct=_chain_completeness_pct(env)))
    gates = LiveMarketGates(clock=comp.clock, data_fresh=_data_fresh,
                            session_valid=_session_valid, buying_power_ok=_buying_power_ok)

    # RSK-04 + RSK-08 + ENT-03 BP, all armed. Also wraps comp.broker so the order
    # cap counts every order any service submits.
    runtime = build_live_runtime(comp, selector=selector, market_gates=gates,
                                 max_entries_per_day=_max_entries(comp),
                                 drift=drift, max_clock_drift_ms=max_drift_ms)

    # ENT-09: the panel's ▶ crosses the identical rails (same ceiling, same book).
    manual = build_manual_entry(
        comp, selector=selector, market_gates=gates,
        max_entries_per_day=_max_entries(comp), drift=drift, max_clock_drift_ms=max_drift_ms,
        day=lambda: datetime.now(timezone.utc).astimezone().date().isoformat())

    return {
        "runtime": runtime,
        "manual": manual,
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
    commands = PanelCommands(comp, manual_entry=live["manual"],
                             preflight_checks=live["preflight_checks"])
    app = create_app(comp.state, comp.events, api_token=token, commands=commands)
    app.state.composition = comp
    app.state.commands = commands
    app.state.session_probe = live["session_probe"]   # DAY-03 clock reading source
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

    @app.post("/broker/connect")
    async def broker_connect() -> dict:
        """Retry the broker session + boot reconcile (token-gated by middleware)."""
        try:
            await comp.connect(account)
            app.state.broker_connected = True
            app.state.broker_error = None
            await _boot_reconcile()
            await _probe_once()   # DAY-03: verify the clock on reconnect too
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
        remaining = _remaining_rows(_todays_entry_times(), now, comp.events,
                                    now.date().isoformat())
        extras = _day_status_extras(remaining, now)
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
