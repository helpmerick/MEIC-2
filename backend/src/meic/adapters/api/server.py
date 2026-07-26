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
import logging
import time as _time
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from fastapi import HTTPException, Query

from meic.application.exit_alerts import ExitAlertRateLimiter, error_key
from meic.application.exit_evaluability import ClockStallDetector, ExitEvaluabilityTracker
from meic.adapters.api.app import _strike_from_symbol
from meic.adapters.logging_setup import configure_logging
from meic.application.clocks import MutableClock
from meic.application.market_calendar import (
    ET,
    RTH_CLOSE,
    is_trading_day,
    next_trading_day,
    trading_day,
    trading_day_str,
)
from meic.application.nyse_holidays import half_days_near, holidays_near, nyse_holidays
from meic.composition.paper import PaperComposition
from meic.composition.runtime import PaperDemoRuntime
from meic.domain.ticks import TickRung, TickTable

# UND-01/UND-02 (v1.86): SPX and RUT share this IDENTICAL tick structure
# (verified 2026-07-21, both Cboe cash-settled index options -- $0.05 below
# $3.00, $0.10 at-or-above) -- see domain/underlying.py's PROFILES for the
# per-underlying citation. This constant is CORRECT for both underlyings this
# phase, so it stays a single shared table (no restructuring this phase, per
# the ratified UND-01..06 build order). FLAG: the /ES phase MUST make this
# per-profile once futures-option tick rules are verified -- do not silently
# reuse this table for /ES when that phase lands.
SPX = TickTable((TickRung(Decimal("3.00"), Decimal("0.05")), TickRung(None, Decimal("0.10"))))
ROOT = Path(__file__).resolve().parents[5]

# FIX-11 (v1.86): the snapshot prime's own worst-case fetch latency --
# `snapshot_chain`'s spot_timeout_s (10) + quote_timeout_s (12). An ad-hoc
# (ENT-11) provisioning pin (FIX-10/FIX-11) must outlast the ENTRY WINDOW the
# selector may retry across (doc 06 `entry_window_seconds`) PLUS this prime
# latency, so a concurrent ~60s health-tick `sync()` can never prune the
# just-provisioned stream mid-fire before the ad-hoc entry either completes
# selection or FILLS (entering the open-entry set and becoming naturally
# wanted). The full TTL is `entry_window_seconds + this` (see
# `_wire_live_day`'s `ad_hoc_pin_ttl_s`), never a bare literal.
_SNAPSHOT_PRIME_WORST_CASE_S = 22.0   # snapshot_chain spot_timeout_s(10) + quote_timeout_s(12)

# 2026-07-14 (server logging from boot): server logs were never written at
# all until 2026-07-13 -- see adapters/logging_setup.py's module docstring
# for the incident (cert-day logs permanently lost) this closes. This
# module's own logger; `configure_logging()` is called once from each real
# entrypoint (`paper_app`/`live_app` below) so a boot logs identically
# however the process is started.
logger = logging.getLogger("meic.server")

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
        # 2026-07-14: every alert (the uniform error-reporting path virtually
        # every supervised background loop already uses -- watchdogs, the
        # quote stream, the day supervisor) is ALSO logged, durably, to the
        # per-boot file. `level` here is the alert's own vocabulary
        # ("critical"/"warning"/"info"), not a Python logging level name --
        # map the ones actually used and fall back to INFO for anything else
        # rather than crash a live alert over an unrecognised label.
        _log_level = {"critical": logging.ERROR, "warning": logging.WARNING,
                     "info": logging.INFO}.get(level, logging.INFO)
        logger.log(_log_level, "ALERT[%s] %s %s", level, message, context)

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

    env = _read_env()
    configure_logging(env, root=ROOT)  # 2026-07-14: logging works however the app is started

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
    manual = ManualEntry(comp, selector, gates,
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


def _cal_stale_after_days(env: dict[str, str]) -> int:
    """CAL-02 `cal_stale_after_days` (doc 06: range 7-365, default 45) -- the
    calendar staleness banner threshold (display-only, CAL-07: never
    blocks). Out-of-range falls back to the spec default, the same
    reject-the-dial convention as `_chain_completeness_pct` above."""
    try:
        raw = int(env.get("MEIC_CAL_STALE_AFTER_DAYS", "45"))
    except ValueError:
        return 45
    return raw if 7 <= raw <= 365 else 45


def _cal_auto_refresh(env: dict[str, str]) -> bool:
    """CAL-09 v1.77 `cal_auto_refresh` (doc 06: bool, default true) -- the
    operator's opt-out to manual-paste-only. Only the literal string
    "false" (case-insensitive) turns it off; anything else (including an
    unset env var) is the safe default, on."""
    return env.get("MEIC_CAL_AUTO_REFRESH", "true").lower() != "false"


def _cal_refresh_fail_alert_days(env: dict[str, str]) -> int:
    """CAL-09 v1.77 `cal_refresh_fail_alert_days` (doc 06: range 1-14,
    default 3) -- consecutive failed refresh days before the persistent
    alert. Out-of-range falls back to the spec default, the same
    reject-the-dial convention as `_cal_stale_after_days` above."""
    try:
        raw = int(env.get("MEIC_CAL_REFRESH_FAIL_ALERT_DAYS", "3"))
    except ValueError:
        return 3
    return raw if 1 <= raw <= 14 else 3


def _event_warning_lead_days(env: dict[str, str]) -> int:
    """CAL-11 v1.84 `event_warning_lead_days` (doc 06: range 0-5, default 3)
    -- how many trading days ahead of an event the Trading tab's dismissable
    proximity warning appears (0 leaves only day-of). Out-of-range falls
    back to the spec default, the same reject-the-dial convention as
    `_cal_stale_after_days` above."""
    try:
        raw = int(env.get("MEIC_EVENT_WARNING_LEAD_DAYS", "3"))
    except ValueError:
        return 3
    return raw if 0 <= raw <= 5 else 3


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


def _exit_eval_interval_ms(env: dict[str, str]) -> int:
    """TPF-03a `exit_eval_interval_ms` (doc 06: range 100-5000, default 250) --
    the MAXIMUM interval between exit evaluations of every armed entry.

    This is NOT an infra polling dial like `MEIC_HEALTH_INTERVAL_S`: it is a
    ratified TRADING parameter, and it is the one the 2026-07-26 defect of
    record was really about. Exit evaluation had exactly one caller -- the 60 s
    health tick, SLEEP-FIRST -- so a breach that began and ended inside one
    window was NEVER OBSERVED, and a persisting breach acted 60-120 s late.
    Out-of-range falls back to the spec default (the same reject-the-dial
    convention as `_max_quote_age_ms` above)."""
    try:
        raw = int(env.get("MEIC_EXIT_EVAL_INTERVAL_MS", "250"))
    except ValueError:
        return 250
    return raw if 100 <= raw <= 5000 else 250


def _exit_unevaluable_alert_s(env: dict[str, str]) -> int:
    """TPF-03d / NFR-08a `exit_unevaluable_alert_s` (doc 06: range 5-600,
    default 60) — how long an armed exit may be unevaluable before an RSK-06
    alert, and the NFR-08a per-distinct-error alert rate limit. Out-of-range
    falls back to the spec default (reject-the-dial, as above)."""
    try:
        raw = int(env.get("MEIC_EXIT_UNEVALUABLE_ALERT_S", "60"))
    except ValueError:
        return 60
    return raw if 5 <= raw <= 600 else 60


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


def _lex_ladder_watchdog_grace_seconds(env: dict[str, str]) -> Decimal:
    """LEX-07 invariant watchdog (2026-07-14) `lex_ladder_grace_seconds`: how
    long a side may sit `ShortStopped` (a genuine stop-out, DCY-03 decay
    excepted) with no `LongSaleStarted` before the watchdog raises its
    CRITICAL alert naming the entry+side -- the class-level fix for the
    2026-07-10 incident (a LEX ladder that silently never ran). Range
    10-300, default 60 -- a ladder legitimately takes a few seconds to start,
    so this must never be so tight it fires on ordinary ladder-start latency.
    Infra polling dial, same class as `MEIC_WATCHDOG_GRACE_SECONDS` /
    `MEIC_SETTLEMENT_LOOKBACK_DAYS` above. Out-of-range falls back to the
    default (the same reject-the-dial convention as `_chain_completeness_pct`
    above)."""
    try:
        raw = Decimal(env.get("MEIC_LEX_LADDER_GRACE_SECONDS", "60"))
    except (ArithmeticError, ValueError):
        return Decimal("60")
    return raw if Decimal("10") <= raw <= Decimal("300") else Decimal("60")


def _decay_buyback_enabled(env: dict[str, str]) -> bool:
    """DCY-01 `decay_buyback_enabled` (doc 06: default true) -- an operator
    kill switch for the whole decay watcher, independent of Stop Trading
    (DCY-01: the watcher continues under Stop Trading; this is the one dial
    that turns it off outright). Only the literal string "false" (any case)
    disables it -- absent/unset/anything else defaults to the safe-and-
    documented `true`, the same reject-the-dial convention as every other
    dial in this module."""
    return env.get("MEIC_DECAY_BUYBACK_ENABLED", "true").strip().lower() != "false"


def _decay_buyback_trigger(env: dict[str, str]) -> Decimal:
    """DCY-01 `decay_buyback_trigger` (doc 06: $0.05-$0.50 step $0.05, default
    $0.05) -- the short's ASK at/below which a buyback fires. Out-of-range
    falls back to the spec default (the same reject-the-dial convention as
    `_watchdog_grace_seconds` above)."""
    try:
        raw = Decimal(env.get("MEIC_DECAY_BUYBACK_TRIGGER", "0.05"))
    except (ArithmeticError, ValueError):
        return Decimal("0.05")
    return raw if Decimal("0.05") <= raw <= Decimal("0.50") else Decimal("0.05")


def _decay_confirmation_evals(env: dict[str, str]) -> int:
    """DCY-01 `decay_confirmation_evals` (doc 06: 1-10, default 2) -- the
    consecutive valid at/below-trigger evaluations required before a buyback
    fires. Out-of-range falls back to the default."""
    try:
        raw = int(env.get("MEIC_DECAY_CONFIRMATION_EVALS", "2"))
    except ValueError:
        return 2
    return raw if 1 <= raw <= 10 else 2


def _decay_unfilled_timeout_seconds(env: dict[str, str]) -> Decimal:
    """DCY-02(3) `decay_unfilled_timeout_seconds` (doc 06: 5-120, default 30)
    -- the re-inflation guard's timeout: a buyback unfilled this long is
    cancelled and the resting stop re-placed. Out-of-range falls back to the
    default."""
    try:
        raw = Decimal(env.get("MEIC_DECAY_UNFILLED_TIMEOUT_SECONDS", "30"))
    except (ArithmeticError, ValueError):
        return Decimal("30")
    return raw if Decimal("5") <= raw <= Decimal("120") else Decimal("30")


def _decay_cutoff_time(env: dict[str, str]) -> dtime:
    """DCY-01 `decay_cutoff_time` (doc 06: ET time, default 15:55) -- no
    buybacks fire at/after this wall-clock time; expiry finishes the job free.
    Parsed the SAME "HH:MM"/"HH.MM" shape `schedule_service._parse_time` uses
    -- an unparseable value falls back to the spec default rather than
    crashing boot (the reject-the-dial convention every other dial here
    follows)."""
    import re as _re

    raw = env.get("MEIC_DECAY_CUTOFF_TIME", "15:55").strip()
    m = _re.fullmatch(r"(\d{1,2})[.:](\d{2})", raw)
    if not m:
        return dtime(15, 55)
    h, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        return dtime(15, 55)
    return dtime(h, mm)


def _es_eod_close_deadline(env: dict[str, str]) -> dtime:
    """EOD-02/UND-03 `eod_close_deadline` (doc 06 §134: ET time > close_time,
    default 15:59) -- the marketable-fallback hard deadline the force-close
    scheduler applies to every MANDATORY-eod-close underlying (today: /ES
    only). Parsed the SAME "HH:MM"/"HH.MM" shape `_decay_cutoff_time` above
    uses; an unparseable value falls back to the spec default rather than
    crashing boot (the reject-the-dial convention every other dial here
    follows)."""
    import re as _re

    raw = env.get("MEIC_EOD_CLOSE_DEADLINE", "15:59").strip()
    m = _re.fullmatch(r"(\d{1,2})[.:](\d{2})", raw)
    if not m:
        return dtime(15, 59)
    h, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        return dtime(15, 59)
    return dtime(h, mm)


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


def _stop_fill_catchup_max_age_days(env: dict[str, str]) -> int:
    """EC-STP-06 (2026-07-14, operator ruling) `stop_fill_catchup_max_age_days`:
    bounds the live stop-fill catch-up (`application/stop_fill_watch.py`,
    `_within_catchup_window`) to entries within this many CALENDAR days of
    TODAY (DAY-03: the ET trading day, computed via `market_calendar.
    trading_day` -- never a wall-clock date).

    THE INCIDENT THIS CLOSES: observed live 2026-07-13 -- the catch-up
    re-evaluated the already-resolved 2026-07-10 entry against TODAY's broker
    positions (which obviously hold nothing three days old), emitting the
    same misleading "operator disposed of it directly -- standing down" INFO
    on every single boot, forever, because the entry's projection never
    reached a terminal state (a since-fixed settlement-capture bug).

    Every entry this bot ever books is 0DTE (STK-01): a GENUINE catch-up --
    matching a resting stop's fill, or resolving an orphaned long's true
    disposition -- is always settled within its own trading day, almost
    always within the very next ~60s health tick. The window here is set
    well past that floor purely so an operator-initiated restart spanning an
    ORDINARY weekend (Friday close -> Monday boot, 3 calendar days) or a long
    holiday weekend never silently drops a real catch-up. Default 5
    (mirroring `_settlement_lookback_days` immediately above -- the same
    class of infra dial, same reject-the-dial convention), range 1-30 (same
    range as `_settlement_lookback_days`): an entry outside even 30 days is
    definitionally stale (0DTE) and its lingering non-terminal projection
    state is a bug to fix at the source, not something more re-evaluation
    could ever resolve. Out-of-range falls back to the default."""
    try:
        raw = int(env.get("MEIC_STOP_FILL_CATCHUP_MAX_AGE_DAYS", "5"))
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


async def _health_tick(app_state, alerts, probe_once) -> None:
    """One GUARDED health-loop tick (v1.74 health-loop guard) — mirrors
    `_supervisor_tick`'s pattern exactly. NFR-02's clock/session-liveness
    probe must survive a tick exception (RSK-06): before this fix, an
    unhandled exception in the health loop's body killed `health_task`
    outright, and the clock/session-liveness reading would go stale forever
    with nothing to say why. Alert once per DISTINCT error — not every
    interval — by latching the last failure's repr on
    `app_state.health_loop_error` (None when healthy)."""
    try:
        await probe_once()
        app_state.health_loop_error = None   # a clean tick clears the latch
    except Exception as exc:  # noqa: BLE001
        err = repr(exc)
        if err != app_state.health_loop_error:
            app_state.health_loop_error = err
            alerts.alert("critical", f"NFR-02: health loop tick failed: {err}")


def _health_task_done_callback(alerts):
    """v1.74 health-loop guard: build the done-callback for `health_task`,
    mirroring `attempt_crash.alert_and_journal_crashed_attempt`'s pattern —
    CRITICAL alert if the supervised task itself ever dies (an exception
    `_health_tick`'s own guard somehow didn't catch, e.g. inside asyncio
    machinery around it), since NFR-02's clock/session-liveness reading would
    then silently stop updating with nothing else watching it. A cancelled
    task (deliberate shutdown, see `_stop_health_loop`) is not a crash and is
    never alerted. The whole callback is wrapped in a bare except — a
    done-callback that itself raises is only logged by asyncio as an
    unhandled exception in the callback machinery, never re-raised into the
    loop, but this guard removes any dependence on that backstop."""
    def _on_done(task) -> None:
        try:
            if task.cancelled():
                return
            exc = task.exception()  # retrieval — must run before anything else can fail
            if exc is not None:
                alerts.alert("critical", f"NFR-02: health_task died: {exc!r}")
        except Exception as cb_exc:  # noqa: BLE001 — a broken callback must not kill the loop
            logger.error("health task done-callback itself failed: %r", cb_exc)
    return _on_done


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


def _mark_expired_sides(events, day: str, *, clock=None) -> None:
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
            at = clock.now().isoformat() if clock is not None else None  # ORD-11 (v1.67)
            events.append(SideExpired(entry_id=entry_id, side=side, at=at))


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
                _mark_expired_sides(comp.events, day, clock=comp.clock)
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
            _mark_expired_sides(comp.events, prior_day, clock=comp.clock)
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


def _underlying_of_symbol(occ_symbol: str | None) -> str:
    """UND-01/UND-04 (v1.86): the PROFILE NAME an OCC leg symbol belongs to,
    resolved from its 6-char OCC root ("SPXW  ..." -> "SPX", "RUTW  ..." ->
    "RUT"). "SPX" for anything unresolvable -- byte-identical to the
    pre-v1.86 SPX-only assumption for every legacy symbol."""
    from meic.domain.underlying import profile_by_root

    if not occ_symbol:
        return "SPX"
    profile = profile_by_root(occ_symbol[:6].strip())
    return profile.name if profile is not None else "SPX"


def _snap_for(source, underlying: str):
    """UND-01/UND-04 (v1.86): resolve the chain snapshot for `underlying`
    from `source`, which may be (checked in this order):

      * the per-underlying `_Snapshots` registry (has `.snapshot_for`) --
        routed per underlying; None when no stream exists for it (an HONEST
        absence -- never another underlying's chain: a RUT entry priced off
        an SPX chain is the exact defect class this routing exists to kill);
      * a legacy single-stream holder (has `.last`) -- every pre-v1.86 test
        fake; returns its one snapshot regardless of underlying;
      * a bare snapshot object, or None -- legacy call shape; returned as-is.
    """
    getter = getattr(source, "snapshot_for", None)
    if getter is not None:
        return getter(underlying)
    if hasattr(source, "last"):
        return source.last
    return source


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
    so it keeps resolving off the snapshot, exactly as today.

    UND-01/UND-04 (v1.86): each leg translates through ITS OWN underlying's
    snapshot (`_snap_for` + the leg symbol's OCC root) — a RUT leg's streamer
    symbol never resolves off an SPX chain's table. Legacy callers passing a
    bare snapshot (every pre-v1.86 test) are byte-identical: `_snap_for`
    returns that same object for every leg."""
    from meic.domain.projection import fold

    day = fold(events)
    symbols: set[str] = set()
    for e in day.entries.values():
        if e.status in _TERMINAL_STATUSES or not e.legs:
            continue
        for leg in e.legs:
            snap_leg = _snap_for(snapshot, _underlying_of_symbol(leg.symbol))
            streamer = _streamer_symbol(snap_leg, leg.symbol, leg.side)
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
    from meic.domain.underlying import profile_for

    def enrich(cards: list[dict]) -> list[dict]:
        now = clock.now() if clock is not None else None
        day = fold(comp.events)
        for card in cards:
            card["live_pnl"] = None
            card["live_pnl_asof"] = None
            if card.get("status") in _TERMINAL_STATUSES:
                continue
            e = day.entries.get(card["entry_id"])
            if e is None or not e.legs:
                continue
            # UND-01/UND-04 (v1.86): THIS entry's own underlying's snapshot,
            # routed via `_snap_for`. An entry whose underlying has no live
            # stream yields the HONEST NULL (existing convention) -- never
            # marks priced off another underlying's chain.
            snap = _snap_for(snaps, getattr(e, "underlying", "SPX"))
            if snap is None or snap.stale:
                continue
            stamps: dict[str, tuple] = {}
            open_costs = _open_side_costs(e, snap, hub=hub, now=now,
                                          max_quote_age_ms=max_quote_age_ms, stamps=stamps)
            if open_costs is None:
                continue  # a still-open side's mark is unavailable -> honest null, never a guess
            contracts = next((leg.qty for leg in e.legs if leg.role == "short"), 1)
            profit = entry_profit_amount(net_credit=e.net_credit, fees=e.fees, stop_fills=e.stop_fills,
                                         recoveries=e.recoveries, open_side_costs=open_costs)
            # UND-02 (v1.86): scaled by THIS entry's OWN profile multiplier
            # (SPX/RUT ×100, /ES ×50), not a hardcoded ×100.
            profile = profile_for(e.underlying)
            multiplier = profile.multiplier if profile is not None else Decimal("100")
            card["live_pnl"] = str(profit * multiplier * contracts)
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
        now = clock.now() if clock is not None else None
        day = fold(comp.events)
        for card in cards:
            e = day.entries.get(card["entry_id"])
            # UND-01/UND-04 (v1.86): THIS entry's own underlying's snapshot
            # (`_snap_for`); no stream for it -> the honest None, never
            # another underlying's chain.
            snap = None if e is None else _snap_for(snaps, getattr(e, "underlying", "SPX"))
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
    # CLS-06: while a partial-close window is open, the fold strips the
    # remainder's SIDES back out of `sides_closed` so the projection shows
    # them open -- but a SHORT on such a side may itself already be flat at
    # the broker (its replace-exit landed; only the sibling long remains in
    # `incomplete_close_legs`). Marking that side "open" here would feed the
    # flat short's stale mid into the TPF/TPT profit% -- a phantom
    # cost-to-close on a position that no longer exists. A short whose
    # symbol is NOT among the journaled remaining legs has completed its
    # exit: treat its side as gone (its realized effect is the journal's
    # job, never a re-mark -- same TPF-05 principle as the sets above).
    # Baseline is byte-identical: `incomplete_close_legs` is empty outside
    # the window and this loop never runs.
    if e.incomplete_close_legs:
        remaining_syms = {r[0] for r in e.incomplete_close_legs}
        for leg in e.legs:
            if leg.role == "short" and leg.symbol not in remaining_syms:
                gone.add(leg.side)
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
        # UND-01/UND-04 (v1.86): routed per THIS entry's underlying.
        snap = _snap_for(snapshots, getattr(e, "underlying", "SPX"))
        return _entry_profit_pct_now(e, snap, hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)

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
    now = clock.now() if clock is not None else None
    # TPF-03b: ONE reading of "now" for the whole pass, in milliseconds.
    # Taken once so every entry in a pass measures its continuous breach
    # against the SAME instant -- two readings inside one pass could put two
    # entries milliseconds apart for no reason, and the elapsed span is the
    # trigger input now, not a counter.
    now_ms = int(now.timestamp() * 1000) if now is not None else int(_time.monotonic() * 1000)
    # TPF-03b(iii): computed ONCE per pass -- a stalled clock is a property
    # of the pass, not of any single entry.
    clock_advanced = _clock_stall(comp).advanced(now_ms)
    for entry_id, e in day.entries.items():
        # TPF-03g: each entry is evaluated INDEPENDENTLY. NFR-08a keeps the
        # LOOP alive, which is a strictly weaker guarantee than keeping the
        # PASS complete -- the 2026-07-20 incident's shape was exactly one
        # throwing path killing everything downstream of it in the same pass,
        # so three armed floors could be silently disabled by one unmarkable
        # leg on a fourth entry. The failure is captured, alerted naming THAT
        # entry, and the pass continues.
        try:
            await _evaluate_one_entry(
                comp, entry_id, e, floors, targets, exit_monitor, commands,
                snapshot=snapshot, hub=hub, now=now, now_ms=now_ms,
                max_quote_age_ms=max_quote_age_ms, clock_advanced=clock_advanced)
        except Exception as exc:  # noqa: BLE001 -- one entry must never blind the others
            _alert_exit_failure(comp, exc, entry_id=entry_id, now=now)



def _exit_evaluability(comp) -> ExitEvaluabilityTracker:
    """TPF-03d's tracker, held on the composition for the same reasons as the
    NFR-08a limiter (see `_exit_alert_limiter`): it must outlive a pass, must
    honour the OPERATOR's dial rather than an import-time default, and must
    not leak state between apps."""
    tracker = getattr(comp, "_exit_evaluability", None)
    if tracker is None:
        window = getattr(comp, "exit_unevaluable_alert_s", None)
        tracker = ExitEvaluabilityTracker(
            alert_after_s=window if window is not None else _exit_unevaluable_alert_s({}))
        comp._exit_evaluability = tracker
    return tracker


def _clock_stall(comp) -> ClockStallDetector:
    detector = getattr(comp, "_clock_stall", None)
    if detector is None:
        detector = ClockStallDetector()
        comp._clock_stall = detector
    return detector


def _alert_unevaluable_exit(comp, surfaced, *, now=None) -> None:
    """TPF-03d/RSK-06: an armed exit unevaluable for the whole threshold."""
    try:
        comp.alerts.alert(
            "critical",
            f"ARMED EXIT UNEVALUABLE for {int(surfaced.seconds)}s on entry "
            f"{surfaced.entry_id} -- its floor/target is NOT being evaluated: "
            f"{surfaced.reason}",
            entry_id=surfaced.entry_id, reason=surfaced.reason,
            seconds=int(surfaced.seconds))
    except Exception:  # noqa: BLE001 -- a broken sink must not kill the pass
        logger.exception("unevaluable-exit alert sink raised for %s", surfaced.entry_id)


def _exit_alert_limiter(comp) -> ExitAlertRateLimiter:
    """NFR-08a's limiter, held on the COMPOSITION.

    It must outlive any single evaluation pass -- a throw every 250 ms that
    reset its own cooldown would flood exactly the channel the limit protects
    -- but it must NOT be module-global. Two reasons, both found the moment it
    was: a module singleton silently ignores the operator's configured
    `exit_unevaluable_alert_s` (it is built at import, before any env is
    read), and it leaks suppression state between independent apps, so one
    app's alert could mute another's. Per-composition is the scope that
    matches the lifetime of the thing being alerted about."""
    limiter = getattr(comp, "_exit_alert_limiter", None)
    if limiter is None:
        window = getattr(comp, "exit_unevaluable_alert_s", None)
        limiter = ExitAlertRateLimiter(
            window_s=window if window is not None else _exit_unevaluable_alert_s({}))
        comp._exit_alert_limiter = limiter
    return limiter

def _alert_exit_failure(comp, exc, *, entry_id: str | None = None, now=None) -> None:
    """NFR-08a: a raised exception from exit evaluation produces a CRITICAL
    alert, rate-limited per distinct error -- never a log line alone.

    The rate limit is part of the rule: at 250 ms an unlimited alert on a
    persistent throw is four per second, and a channel emitting four a second
    is one the operator mutes -- which is indistinguishable from the silence
    NFR-08a exists to end.

    Never raises. An alert sink that fails must not be the thing that kills
    the evaluation pass it was reporting on."""
    key = error_key(entry_id or "pass", exc)
    now_s = now.timestamp() if now is not None else _time.monotonic()
    if not _exit_alert_limiter(comp).should_send(key, now_s=now_s):
        logger.warning("exit evaluation failed (alert rate-limited): %r", exc)
        return
    scope = f"entry {entry_id}" if entry_id else "the evaluation pass"
    try:
        comp.alerts.alert(
            "critical",
            f"EXIT EVALUATION FAILED for {scope} -- floors/targets for it are NOT "
            f"being evaluated: {type(exc).__name__}: {exc}",
            entry_id=entry_id, error=repr(exc))
    except Exception:  # noqa: BLE001 -- a broken sink must not kill the pass
        logger.exception("exit evaluation failed AND the alert sink raised: %r", exc)
    else:
        logger.warning("exit evaluation failed for %s: %r", scope, exc)


async def _evaluate_one_entry(comp, entry_id, e, floors, targets, exit_monitor, commands,
                              *, snapshot, hub, now, now_ms, max_quote_age_ms,
                              clock_advanced: bool = True) -> None:
    """ONE entry's floor/target evaluation (TPF-03g's unit of isolation).

    Extracted so the pass-level loop above has a single place to catch per
    entry: an inline try/except around a multi-branch body invites a later
    edit to land outside it, and the isolation guarantee would silently
    narrow."""
    level_floor, level_target = floors.get(entry_id), targets.get(entry_id)
    if level_floor is None and level_target is None:
        return
    if e.status in _TERMINAL_STATUSES:
        exit_monitor.forget(entry_id)
        _exit_evaluability(comp).forget(entry_id)
        return
    # UND-01/UND-04 (v1.86): THIS entry's own underlying's snapshot
    # (`_snap_for` accepts the per-underlying registry, a legacy holder,
    # or a bare snapshot -- every pre-v1.86 caller unchanged). No stream
    # for the entry's underlying -> stale -> pause, never another chain.
    snap_e = _snap_for(snapshot, getattr(e, "underlying", "SPX"))
    stale = snap_e is None or snap_e.stale
    profit_pct = None if stale else _entry_profit_pct_now(
        e, snap_e, hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)
    entry_stale = stale or profit_pct is None

    # TPF-03d: an armed exit that cannot be evaluated is SURFACED. An
    # unmarkable leg makes profit_pct None, which reads downstream as "no
    # breach" -- indistinguishable from "not breached", so the operator
    # believes a floor is protecting them when it has not been evaluated for
    # hours. TPF-03b(iii) rides the same path: a clock that is not ADVANCING
    # cannot accumulate a continuous breach, so it is the same observable and
    # earns the same treatment.
    if not clock_advanced:
        unevaluable_reason = ("the evaluation clock is not advancing -- no continuous "
                              "breach can accumulate, so nothing can ever confirm")
    elif entry_stale:
        unevaluable_reason = ("the entry cannot be fully marked (stale snapshot, or an "
                              "open side with no usable mark)")
    else:
        unevaluable_reason = ""
    surfaced = _exit_evaluability(comp).observe(
        entry_id, evaluable=not unevaluable_reason, reason=unevaluable_reason,
        now_s=now_ms / 1000.0)
    if surfaced is not None:
        _alert_unevaluable_exit(comp, surfaced, now=now)

    if level_floor is not None:
        if exit_monitor.evaluate_floor(entry_id, profit_pct=profit_pct,
                                       level=int(level_floor), stale=entry_stale,
                                       now_ms=now_ms):
            await commands.close_as(entry_id, "take_profit")
            return  # the entry is now closed — skip its target this pass

    if level_target is not None:
        if e.sides_stopped:  # TPT-05: permanent disarm
            exit_monitor.disarm_target(entry_id)
        elif exit_monitor.evaluate_target(entry_id, profit_pct=profit_pct,
                                          level=int(level_target), stale=entry_stale,
                                          now_ms=now_ms):
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

    if snapshot is None:
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
        # UND-01/UND-04 (v1.86): routed per THIS entry's underlying (see
        # `_evaluate_exits_once`); `_entry_profit_pct_now` itself None/stale-
        # gates the resolved snapshot, so a missing/stale stream still means
        # "skip this entry", exactly the pre-v1.86 top-level gate's effect.
        snap_e = _snap_for(snapshot, getattr(e, "underlying", "SPX"))
        profit_pct = _entry_profit_pct_now(e, snap_e, hub=hub, now=now, max_quote_age_ms=max_quote_age_ms)
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

    D8b (v1.82, RPT-17's counterfactual): a CLOSED entry keeps receiving the
    SAME per-tick sample too, as long as it (1) closed TODAY -- entry_day(...)
    must match the ET trading day this tick's own snapshot instant falls on,
    never a prior day -- and (2) this tick is still at or before the 16:00
    ET close (inclusive: see `past_close`'s own comment below for why the
    boundary tick itself must still count). Both conditions read off
    `snapshot.taken_at` alone, so this is bounded, day-scoped, and
    replay-safe, and it fetches nothing new: every mark below still comes
    from the identical live snapshot the open-entry path already reads.
    `today_str`/`past_close` are None/False (never True) when `at` itself is
    unavailable, so the extension simply does not apply that tick -- the
    pre-existing open-entry sampling is unaffected.
    """
    from meic.domain.events import EntryMarkSample
    from meic.domain.projection import fold
    from meic.reporting.folds import entry_day

    if snapshot is None:
        return
    day = fold(comp.events)
    for e in day.entries.values():
        if not e.legs:
            continue
        # UND-01/UND-04 (v1.86): THIS entry's own underlying's snapshot
        # (`_snap_for` -- registry, legacy holder, or bare snapshot all
        # accepted; a bare snapshot reproduces the pre-v1.86 behaviour
        # byte-identically, since `at`/`today_str`/`past_close` below then
        # compute from the same one object for every entry). A missing or
        # stale stream samples NOTHING for that entry -- D10's honest gap.
        snap_e = _snap_for(snapshot, getattr(e, "underlying", "SPX"))
        if snap_e is None or snap_e.stale:
            continue
        at = getattr(snap_e, "taken_at", None)
        at_iso = at.isoformat() if at is not None else None
        today_str = trading_day(at).isoformat() if at is not None else None
        # Strictly GREATER than the close (never >=): a tick landing AT
        # 16:00:00 ET is the last one D8b must still capture -- RPT-17's
        # Unmanaged counterfactual specifically wants "the recorded 16:00
        # spread value", and the health-tick cadence is not guaranteed to
        # land on any OTHER exact second, so excluding the boundary tick
        # itself could leave the counterfactual with no usable sample at all
        # on an otherwise-normal day.
        past_close = at is not None and at.astimezone(ET).time() > RTH_CLOSE
        if e.status in _TERMINAL_STATUSES:
            # D8b: sample a closed entry only if it closed TODAY and the
            # 16:00 ET close hasn't passed yet -- see the docstring above.
            if today_str is None or entry_day(e.entry_id) != today_str or past_close:
                continue
        by_side: dict[str, dict] = {"PUT": {}, "CALL": {}}
        for leg in e.legs:
            by_side.setdefault(leg.side, {})[leg.role] = leg
        put_short, put_long = by_side["PUT"].get("short"), by_side["PUT"].get("long")
        call_short, call_long = by_side["CALL"].get("short"), by_side["CALL"].get("long")
        put_short_mid = (_leg_mid(snap_e.put_side, Decimal(_strike_from_symbol(put_short.symbol)))
                         if put_short else None)
        put_long_mid = (_leg_mid(snap_e.put_side, Decimal(_strike_from_symbol(put_long.symbol)))
                        if put_long else None)
        call_short_mid = (_leg_mid(snap_e.call_side, Decimal(_strike_from_symbol(call_short.symbol)))
                          if call_short else None)
        call_long_mid = (_leg_mid(snap_e.call_side, Decimal(_strike_from_symbol(call_long.symbol)))
                        if call_long else None)
        spot = getattr(snap_e, "spot", None)
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
    # UND-01/UND-04 (v1.86): pass the holder itself -- `_open_leg_symbols`
    # routes each leg through its own underlying's snapshot (`_snap_for`
    # accepts the registry or a legacy `.last` holder identically).
    symbols = _open_leg_symbols(comp.events, snaps)
    if not symbols:
        await asyncio.sleep(idle_seconds)
        return
    gen = hub.open_generation()
    async for q in feed.quotes(sorted(symbols)):
        hub.apply_tick(q, generation=gen)
        # Recomputed against the CURRENT snapshot every tick: a refreshed
        # snapshot can change the strike->streamer map, not just the open book.
        if _open_leg_symbols(comp.events, snaps) != symbols:
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

        # UND-01/UND-04 (v1.86): translate through THIS leg's own underlying's
        # snapshot (resolved from the OCC root -- "RUTW ..." -> RUT); a leg
        # whose underlying has no live stream resolves no streamer symbol ->
        # stale=True -> the breach clock pauses (DAT-02), never a mark off
        # another underlying's chain.
        snap = _snap_for(snaps, _underlying_of_symbol(leg.symbol))
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


async def _lex_ladder_watchdog_pass(comp, wd, *, now) -> None:
    """LEX-07 (2026-07-14): one pass of the LEX-ladder invariant watchdog --
    purely a fold over `comp.events` (see `application/lex_ladder_watchdog.py`
    for the invariant and the DCY-03 exception). Deliberately NOT shaped like
    `_stop_watchdog_pass` above: that pass needs a live QuoteHub mark and the
    open-short-leg frame because it is a SECOND TRIGGER layer; this one needs
    nothing but the journal and the wall clock, because it is a WATCHDOG OVER
    THE JOURNAL ITSELF -- it must keep working even when every other live
    service (QuoteHub, the LEX ladder, the broker connection) is unwired,
    unreachable, or dead, which is exactly the blind spot it exists to close."""
    wd.observe(comp.events, now=now)


async def _run_lex_ladder_watchdog_loop(comp, wd, alerts, *, clock,
                                        idle_seconds: float = 5.0) -> None:
    """LEX-07 (2026-07-14): supervises `_lex_ladder_watchdog_pass` forever --
    same shape as `_run_stop_watchdog_loop` above (one supervised background
    task, created unconditionally at startup, cancelled on shutdown). NEVER
    crashes the app: any failure is alerted and the loop simply tries again
    next tick. Runs regardless of broker connectivity (unlike the stop
    watchdog, this pass touches no broker/hub at all -- only the journal)."""
    while True:
        try:
            await _lex_ladder_watchdog_pass(comp, wd, now=clock.now())
        except Exception as exc:  # noqa: BLE001 -- must never crash the app
            alerts.alert("warning", f"LEX-07 ladder watchdog pass failed: {exc!r}")
        await asyncio.sleep(idle_seconds)


async def _run_force_close_scheduler_loop(comp, scheduler, alerts, *, clock,
                                          idle_seconds: float = 5.0) -> None:
    """UND-03/F3 (v1.86 /ES Stage 2): supervises
    `ForceCloseScheduler.run_once` forever -- same shape as
    `_run_lex_ladder_watchdog_loop` above (one supervised background task,
    created unconditionally at startup, cancelled on shutdown). NEVER crashes
    the app: a broker hiccup mid-close is alerted and the next tick simply
    re-derives what's still open from the journal and retries -- exactly the
    idempotent-retry shape `ForceCloseScheduler.run_once`'s own docstring
    describes. Runs unconditionally regardless of whether any /ES entry has
    ever been placed -- the policy-table lookup inside `run_once` is what
    makes this scheduler completely inert for an SPX/RUT-only day."""
    while True:
        try:
            await scheduler.run_once(clock.now())
        except Exception as exc:  # noqa: BLE001 -- must never crash the app
            # SETTLEMENT-SAFETY (2026-07-21 final review): the loop's OWN
            # except-body must never itself kill the loop. A failing
            # `alerts.alert` here (a broken sink) would otherwise propagate
            # out of the `except` and end the `while True` silently -- and a
            # dead force-close loop means every open /ES rides into
            # settlement with no protection. Swallow an alert failure to a
            # logger, exactly as every other alert-sink use in this module
            # treats its sink as best-effort.
            try:
                alerts.alert("warning", f"UND-03/F3 force-close scheduler pass failed: {exc!r}")
            except Exception as alert_exc:  # noqa: BLE001
                logger.error("force-close loop alert sink failed: %r", alert_exc)
        await asyncio.sleep(idle_seconds)


def _force_close_task_done_callback(alerts):
    """SETTLEMENT-SAFETY (2026-07-21 final review): the done-callback for
    `force_close_scheduler_task`, mirroring `_health_task_done_callback`
    exactly. The force-close loop's `except Exception` cannot catch a
    BaseException (or anything asyncio raises AROUND `run_once`), and a
    silently dead force-close loop is the single worst F3 outcome -- every
    open /ES entry rides into settlement with ZERO alert. This is the same
    death detection every other safety-critical supervised loop already has
    (health_task's done-callback, the ENT-10 day task's `day_task_failed`
    latch). A cancelled task (deliberate shutdown, see
    `_stop_force_close_scheduler_loop`) is not a crash and is never alerted.
    The whole callback is wrapped in a bare except so a broken callback can
    never itself become an unhandled crash."""
    def _on_done(task) -> None:
        try:
            if task.cancelled():
                return
            exc = task.exception()  # retrieval — must run before anything else can fail
            if exc is not None:
                alerts.alert(
                    "critical",
                    "RSK-06: UND-03/F3 force-close scheduler task DIED -- /ES "
                    f"force-close protection is DOWN, open /ES entries risk "
                    f"settlement/assignment: {exc!r}")
        except Exception as cb_exc:  # noqa: BLE001 — a broken callback must not crash anything
            logger.error("force-close task done-callback itself failed: %r", cb_exc)
    return _on_done


async def _decay_watcher_pass(
    comp, watchers: dict, active: dict, hub, snaps, alerts, *, now,
    max_quote_age_ms: int, buyback_trigger: Decimal, confirmation_evals: int,
    unfilled_timeout_seconds: Decimal, cutoff_time: dtime, enabled: bool,
    fee_model, clock, flatten_in_progress, suspended: dict,
) -> None:
    """DCY-01..04 (2026-07-14): one pass of the decay watcher over every OPEN
    short with a resting stop placed -- the SAME `_open_short_legs` frame
    `_stop_watchdog_pass`/the EC-STP-06 catch-up already drive, fed a LIVE
    QuoteHub mark through the SAME `_streamer_symbol` seam (NFR-04: a decay
    decision must never fire off a 30-60s snapshot).

    `DecayWatcher` was written, unit-tested (tests/application/test_tpf_dcy.py,
    test_decay_watcher_live_shaped.py) and race-guarded, but never constructed
    anywhere outside its own tests -- the seventh exists-but-unwired instance
    (NFR-07). This closes it, on the same supervised-background-task shape as
    every watcher above.

    One `DecayWatcher` PER TRACKED SHORT (`watchers`, keyed (entry_id, side),
    owned by the caller and persisted across passes) -- its `evaluate()`
    confirmation counter is a single scalar, exactly the one-instance-per-side
    shape its own unit tests already use; sharing one instance across two
    sides would conflate their eval counts.

    `active` (owned by the caller) tracks a side with a buyback already
    resting: `{"buyback_id", "trigger", "resting_stop_id", "symbol",
    "contracts", "placed_at"}`. A side with an entry here is NEVER re-evaluated
    for a fresh trigger -- it is only driven through DCY-02(3)'s re-inflation
    guard until the guard reprotects it or the fill is detected. That FILL
    DETECTION is deliberately NOT this pass's job: `DecayBuybackPlaced` (which
    `DecayWatcher.buyback()` journals at placement, STP-08a v1.61) is already
    picked up by `stop_fill_watch.py`'s existing, already-wired detection loop
    (`_decay_buyback_specs`/`_resolve_by_order_id`), which journals the
    ShortStopped(initiator="decay")+EntryClosed(initiator="decay") completion
    itself. Calling `DecayWatcher.complete()` from here too would double-
    journal the same close -- so this pass never calls it.

    DCY-01's mode gate: MANUAL mode (UC-08) and OWN-06 SUSPENDED are, as of
    v1.67, states no code path in this repo can ever place an entry into --
    UC-08 has no command surface and OWN-06's `ForeignReduction` event is
    never appended anywhere (grep-confirmed). Passing `mode="AUTO"` here is
    therefore honest given today's code, not a guess -- flagged in the wiring
    report as a real gap for the operator, not silently assumed away. The
    watcher will respect either state automatically the day that plumbing
    exists, with no change needed here.

    KNOWN LIMITATION (not solved by this wiring, flagged rather than silently
    dropped): `active`/`watchers` are in-memory and do not survive a process
    restart mid-buyback. A crash between `buyback()` cancelling the resting
    stop and this side's fill being detected would, on restart, see
    `_open_short_legs` still list the (stale) StopPlaced spec and could
    attempt a second buyback beside the one already resting at the broker.
    REC-03-style boot reconciliation for an in-flight decay buyback is out of
    this task's scope."""
    if not enabled:
        return
    from meic.application.decay_watcher import DecayWatcher
    from meic.application.market_calendar import ET as _ET
    from meic.application.stop_fill_watch import _open_short_legs
    from meic.domain.events import StopPlaced

    # DCY-01 (last sentence): the suspension holds only "for the remainder of
    # the stop-trading state" -- a fresh (non-stop-trading) tick clears it.
    if not comp.state.stop_trading:
        suspended["value"] = False

    now_et = now.astimezone(_ET) if now.tzinfo is not None else now
    open_shorts = _open_short_legs(comp.events)   # ONE fold of the log this tick, not two
    open_now = {(entry_id, side) for entry_id, side, _leg, _spec in open_shorts}

    # A side that left the open-short-with-a-stop frame has nothing left to
    # accumulate -- drop its bookkeeping (same convention as `last_ticked` in
    # `_stop_watchdog_pass` above).
    for key in set(watchers) - open_now:
        watchers.pop(key, None)
    for key in set(active) - open_now:
        active.pop(key, None)

    flatten_now = bool(flatten_in_progress())

    for entry_id, side, leg, spec in open_shorts:
        key = (entry_id, side)
        # UND-01/UND-04 (v1.86): translate through THIS leg's own underlying's
        # snapshot (same routing as `_stop_watchdog_pass` -- no stream means
        # stale, never another underlying's chain).
        snap = _snap_for(snaps, _underlying_of_symbol(leg.symbol))
        streamer = _streamer_symbol(snap, leg.symbol, side)
        quote = hub.mark(streamer) if streamer else None
        stale = quote is None or quote.is_stale(now, max_quote_age_ms)
        ask = quote.ask if quote is not None else Decimal("0")

        in_flight = active.get(key)
        if in_flight is not None:
            # DCY-02(3): re-inflation guard for an already-placed buyback --
            # never a fresh trigger evaluation while one is resting.
            #
            # DCY-01 "never while a Flatten All is executing" applies to the
            # WHOLE watcher, not just a fresh trigger: while a flatten runs,
            # `assemble_close_inputs` (composition/close_assembly.py) folds a
            # working decay buyback into CloseEntry's own resting_stop_ids and
            # replaces it through the SAME race-safe path an ordinary stop
            # gets -- so the flattening close is what resolves this leg. The
            # guard stands down rather than racing that replace() with its own
            # cancel/re-place of the same order.
            if flatten_now:
                continue
            elapsed = (now - in_flight["placed_at"]).total_seconds()
            unfilled = elapsed >= float(unfilled_timeout_seconds)
            if unfilled or (not stale and ask > buyback_trigger):
                # Review finding (2026-07-14, BLOCKING): `reinflation_guard`'s own
                # race check only looks at `fills_since` -- it has no way to see
                # that a CONCURRENT close (manual/TPF/TPT/EOD, none of which set
                # `flatten_in_progress`) already REPLACED this exact buyback order
                # via CloseEntry's own race-safe path (close_assembly.py now
                # routes a working decay order through there). Cancelling an
                # already-REPLACED order and submitting a fresh stop on top of a
                # leg CloseEntry just closed would rest a PHANTOM stop on a flat
                # leg -- unprotected-side machinery would never see it, and a
                # later trigger would open an unintended long. Re-confirm the
                # buyback is STILL actually resting at the broker, via broker
                # truth (not this module's own bookkeeping), before touching it
                # at all -- if it is gone, someone else already resolved this
                # side; drop our bookkeeping and do nothing further.
                still_resting = any(
                    getattr(o, "order_id", None) == in_flight["buyback_id"]
                    and getattr(o, "status", "WORKING") in ("WORKING", "PARTIAL", "live", "received")
                    for o in await comp.broker.working_orders())
                if not still_resting:
                    active.pop(key, None)
                    continue

                watcher = watchers.setdefault(key, DecayWatcher(
                    broker=comp.broker, events=comp.events,
                    decay_buyback_trigger=buyback_trigger,
                    decay_confirmation_evals=confirmation_evals,
                    fee_model=fee_model, clock=clock))
                try:
                    outcome = await watcher.reinflation_guard(
                        entry_id=entry_id, side=side, buyback_id=in_flight["buyback_id"],
                        resting_stop_id=in_flight["resting_stop_id"], current_ask=ask,
                        unfilled=unfilled, symbol=leg.symbol, trigger=in_flight["trigger"],
                        contracts=in_flight["contracts"])
                except Exception as exc:  # noqa: BLE001 -- must never crash the app
                    alerts.alert(
                        "critical",
                        f"DCY-02.3 re-inflation guard failed for {entry_id}/{side}: {exc!r}")
                    # DCY-01: a failed re-placement while stop-trading suspends
                    # the watcher for the remainder of the stop-trading state --
                    # never retry blind protection machinery.
                    if comp.state.stop_trading:
                        suspended["value"] = True
                    continue
                if outcome.startswith("REPROTECTED:"):
                    # Review finding (BLOCKING): the re-placed stop's own id was
                    # being discarded -- `_stop_specs` (stop_fill_watch.py) keeps
                    # whichever StopPlaced is JOURNALED LAST, so with nothing
                    # appended here it silently kept pointing at the OLD,
                    # cancelled stop forever: a genuine fill on the NEW stop
                    # would then be invisible to fill detection (REC-02: the
                    # journal is authoritative), and STP-03b's own watchdog would
                    # key off a dead order id too. Journal it, exactly like the
                    # original placement did.
                    new_stop_id = outcome.split(":", 1)[1]
                    comp.events.append(StopPlaced(
                        entry_id=entry_id, side=side, trigger=in_flight["trigger"],
                        broker_order_id=new_stop_id))
                    active.pop(key, None)
                elif outcome != "BUYBACK_STILL_LIVE":
                    active.pop(key, None)  # already filled -- nothing left to track here
            continue  # a side with an in-flight buyback is never re-evaluated for a NEW trigger

        watcher = watchers.setdefault(key, DecayWatcher(
            broker=comp.broker, events=comp.events, decay_buyback_trigger=buyback_trigger,
            decay_confirmation_evals=confirmation_evals, fee_model=fee_model, clock=clock))

        if not watcher.gate_allows(now_time=now_et.time(), cutoff_time=cutoff_time,
                                    mode="AUTO", flatten_in_progress=flatten_now,
                                    watcher_suspended=suspended["value"]):
            continue

        if not spec.broker_order_id:
            continue  # nothing confirmed to cancel yet -- never spend a confirmation
                       # eval on a tick that could not act on it even if it fired

        if not watcher.evaluate(ask=ask, stale=stale):
            continue

        try:
            outcome = await watcher.buyback(
                entry_id=entry_id, side=side, resting_stop_id=spec.broker_order_id,
                symbol=leg.symbol, contracts=leg.qty)
        except Exception as exc:  # noqa: BLE001 -- must never crash the app
            alerts.alert("warning", f"DCY-02 buyback failed for {entry_id}/{side}: {exc!r}")
            continue
        if outcome == "STOP_FILLED_RUN_LEX":
            continue  # DCY-02(1): it was a real stop-out; EC-STP-06/LEX already owns it
        active[key] = {"buyback_id": outcome, "trigger": spec.trigger,
                        "resting_stop_id": spec.broker_order_id, "symbol": leg.symbol,
                        "contracts": leg.qty, "placed_at": now}


async def _run_decay_watcher_loop(
    comp, hub, snaps, alerts, *, clock, max_quote_age_ms: int, buyback_trigger: Decimal,
    confirmation_evals: int, unfilled_timeout_seconds: Decimal, cutoff_time: dtime,
    enabled: bool, fee_model, flatten_in_progress, idle_seconds: float = 5.0,
    connected=lambda: True, watchers: dict | None = None, active: dict | None = None,
) -> None:
    """DCY-01..04 (2026-07-14): supervises `_decay_watcher_pass` forever --
    same shape as `_run_stop_watchdog_loop` above (one supervised background
    task, created unconditionally at startup, cancelled on shutdown). NEVER
    crashes the app: any failure is alerted and the loop tries again next
    tick -- a missed pass is never worse than the watcher not existing at all,
    and the resting broker stop stays PRIMARY and bot-independent regardless
    of this loop's health.

    `watchers`/`active` default to fresh dicts but may be supplied by the
    caller (`live_app()` passes `app.state.decay_watchers`/
    `app.state.decay_watcher_active`) so the REAL, ticking bookkeeping is
    reachable from outside the loop's closure -- for the NFR-07 wiring
    registry and tests, never a decorative copy nobody reads."""
    if watchers is None:
        watchers = {}
    if active is None:
        active = {}
    suspended: dict[str, bool] = {"value": False}
    while True:
        if not connected():
            await asyncio.sleep(idle_seconds)
            continue
        try:
            await _decay_watcher_pass(
                comp, watchers, active, hub, snaps, alerts, now=clock.now(),
                max_quote_age_ms=max_quote_age_ms, buyback_trigger=buyback_trigger,
                confirmation_evals=confirmation_evals,
                unfilled_timeout_seconds=unfilled_timeout_seconds, cutoff_time=cutoff_time,
                enabled=enabled, fee_model=fee_model, clock=clock,
                flatten_in_progress=flatten_in_progress, suspended=suspended)
        except Exception as exc:  # noqa: BLE001 -- must never crash the app
            alerts.alert("warning", f"DCY-01..04 decay watcher pass failed: {exc!r}")
        await asyncio.sleep(idle_seconds)


def _wire_live_day(comp, env: dict[str, str], *, flatten_in_progress: Callable[[], bool]) -> dict:
    """Assemble the live trading day: selector, gates, runtime, ▶, pre-flight.

    Thin: every decision that could leave a SAFETY RAIL unarmed lives in
    composition/live_wiring.py, where tests/composition/test_live_wiring.py asserts
    on it directly. That test exists because this function's predecessor built a
    LiveRuntime with max_day_risk, order_cap and buying_power all left at None,
    and threw the composed schedule rows away — while the paper composition and
    every unit test had all of it armed.

    `flatten_in_progress` (RSK-01a, v1.68 constant-signal fix): REQUIRED, no
    default. `LiveMarketGates.for_live()` itself still defaults this input to
    a dead `lambda: False` for callers with no real signal (e.g. paper/tests);
    THIS function, wiring the real live day, must never be one of them — the
    caller (`live_app()`) supplies a late-bound cell because the real signal
    (`PanelCommands.flatten_in_progress`) is only constructed AFTER this
    function runs. Requiring the argument here (vs. silently accepting the
    dataclass default) is what makes forgetting to pass it a loud call-site
    error instead of a silent green gate.
    """
    from meic.application.calendar_store import CalendarStore
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

    # FIX-11 (v1.86): the ad-hoc (ENT-11) provisioning-pin TTL -- the entry
    # window the selector may retry across (doc 06 `entry_window_seconds`)
    # PLUS the snapshot prime's own worst-case fetch latency. Long enough that
    # a just-`ensure()`d ad-hoc stream survives every concurrent ~60s
    # health-tick `sync()` until the fire's selection completes (and, on fill,
    # the entry becomes wanted via the open-entry set). Read from the SAME env
    # dial the selector's own retry window uses -- never a second source.
    ad_hoc_pin_ttl_s = float(_entry_window_seconds(env)) + _SNAPSHOT_PRIME_WORST_CASE_S

    # DAY-03 (v1.48): drift is measured against the BROKER's Date header on the
    # ~60 s session probe — no env var, no NTP. Starts unverified (infinite drift),
    # so entries are blocked until the first probe lands; a reading older than 300 s
    # is treated as unverified too. The session probe below feeds it.
    drift = BrokerClockProbe()

    # DAT-04a v1.80: the halt-signal provider seam (TradingStatusStore, fed by
    # a piggybacked Profile subscription) is RETIRED — module deleted, gate
    # input removed from LiveMarketGates, never stubbed. Halt protection is
    # now carried entirely by the freshness gates (DAT-02/STK-04/STK-10). See
    # composition/live_gates.py's module docstring for the full ruling.

    # CAL-01..08 (doc 11, v1.71): the tag/rule store is a pure fold over the
    # SAME shared event log everything else journals to -- REC-07's own
    # inventory extension needs no new persistence path (see
    # application/calendar_store.py's module docstring).
    calendar_store = CalendarStore(comp.events, comp.clock)

    class _SnapshotStream:
        """One underlying's chain-snapshot stream (UND-01/UND-04 v1.86).
        Starts STALE: unknown freshness is never 'fresh'. Holds the snapshot
        ITSELF (`.last`) — FEATURE 3 (live P/L card) reads marks off it
        rather than opening a second subscription; it refreshes on the same
        ~60s health-loop cadence as everything else that reads `.stale`."""

        def __init__(self, profile) -> None:
            self.profile = profile
            self.stale = True
            self.last = None

        async def take(self):
            from meic.adapters.dxlink.chain_snapshot import snapshot_chain
            # v1.51: no band_points — the subscription span is an internal
            # constant (SUBSCRIBE_SPAN_PTS); the STK-10 gate is trade-relative.
            # `now` stamps the reading off the injected clock, never
            # datetime.now() (DAY-03). DAT-04a v1.80: no more piggybacked
            # trading-status Profile subscription here — the input is retired.
            # UND-04: the profile's OWN option root + index symbol — the exact
            # call shape the RUT chain cert (tests/contract/
            # test_rut_chain_cert.py) proves live.
            snap = await snapshot_chain(comp.broker._session,
                                        underlying=self.profile.option_root,
                                        index_symbol=self.profile.index_symbol,
                                        now=comp.clock.now)
            self.stale = snap.stale
            self.last = snap
            return snap

    class _Snapshots:
        """UND-01/UND-04 (v1.86): the PER-UNDERLYING chain-snapshot registry.

        Streams exist for exactly the set {underlyings of the day's armed
        schedule rows} ∪ {underlyings of currently-open entries} — resolved
        from `comp.state.entry_schedule` + the event-log fold on every
        `sync()`, so an armed-schedule edit or a newly-opened/terminal entry
        adjusts the set on the next probe/lookup; an underlying with no armed
        row and no open entry gets NO chain fetch. When that set is EMPTY
        (nothing armed, nothing open — e.g. a disarmed boot), the SPX default
        stream stands in so every legacy surface (the UC-02 pre-flight data
        probe, the panel's spot readout, `.last`/`.stale` consumers) keeps
        working exactly as before v1.86.

        Legacy single-stream surface, kept deliberately: `.last`/`.stale`
        read (and, for tests that stub them, WRITE) the SPX/default stream —
        every pre-v1.86 consumer and test fixture is unchanged. New
        consumers route per underlying via `snapshot_for`/`provider_for`
        (`_snap_for` + the selector's `snapshot_router`).

        FIX-7 (perf, 2026-07-21): `sync()` (which folds the whole event log
        via `_wanted()`) runs ONLY inside `take()` — the health-tick/warm-up
        probe cadence. Every LOOKUP (`snapshot_for`/`provider_for`/
        `for_underlying`) is a plain dict read against the CURRENT stream
        map, so the per-quote-tick / per-open-leg hot paths never fold the
        (all-day-accreting) journal. A lookup that races ahead of the first
        `take()` (a stream not built yet) returns None -> the caller
        fail-closes (`no_chain_stream` skip / honest null), and the newly
        wanted underlying gets its stream at the very next probe's take()."""

        def __init__(self) -> None:
            import time as _time

            self._streams: dict[str, _SnapshotStream] = {}
            # FIX-11 (v1.86): short-lived AD-HOC provisioning pins -- profile
            # name -> monotonic EXPIRY. `_wanted()` unions the live pins so a
            # concurrent health-tick `sync()` never prunes a stream `ensure()`
            # is mid-priming (or just provisioned) for an ad-hoc fire that has
            # not yet entered the open-entry set. ONE monotonic clock
            # (`self._now`), injectable so the race/expiry tests advance it
            # deterministically without real sleeps -- never mixed with any
            # other time source.
            self._pinned: dict[str, float] = {}
            self._now = _time.monotonic
            self._pin_ttl_s = ad_hoc_pin_ttl_s   # entry_window + snapshot-prime worst case

        def _live_pins(self, now: float) -> set[str]:
            """FIX-11: pinned underlyings whose pin has NOT yet expired."""
            return {n for n, exp in self._pinned.items() if exp > now}

        def _wanted(self) -> set[str]:
            from meic.domain.projection import fold
            from meic.domain.underlying import profile_for as _profile_for

            names: set[str] = set()
            for row in (comp.state.entry_schedule or []):
                if isinstance(row, dict):
                    names.add(str(row.get("underlying") or "SPX"))
            for e in fold(comp.events).entries.values():
                if e.legs and e.status not in _TERMINAL_STATUSES:
                    names.add(getattr(e, "underlying", "SPX"))
            # FIX-11: union the live ad-hoc pins so an in-flight/just-
            # provisioned ad-hoc stream survives a concurrent sync().
            names |= self._live_pins(self._now())
            names = {n for n in names if _profile_for(n) is not None}
            return names or {"SPX"}   # legacy-compat fallback (see class docstring)

        def sync(self) -> None:
            """Reconcile streams to the wanted set: build the missing, drop
            the no-longer-wanted (an open entry keeps its underlying wanted
            until terminal, so a mid-day stream never vanishes under a
            position). FIX-7: called ONLY from `take()` (the probe cadence),
            never from a lookup.

            FIX-11: expired ad-hoc pins are dropped FIRST, so an unfilled
            ad-hoc stream is pruned once its pin lapses (FIX-10's cleanup,
            just deferred past the fire window); a still-live pin keeps its
            stream via the `_wanted()` union."""
            from meic.domain.underlying import PROFILES as _PROFILES

            now = self._now()
            for name in [n for n, exp in self._pinned.items() if exp <= now]:
                del self._pinned[name]                       # FIX-11: expire lapsed pins

            wanted = self._wanted()
            for name in wanted - set(self._streams):
                self._streams[name] = _SnapshotStream(_PROFILES[name])
            for name in set(self._streams) - wanted:
                del self._streams[name]

        async def ensure(self, underlying: str) -> None:
            """FIX-10 (v1.86, 2026-07-21): the AD-HOC (ENT-11) analog of the
            scheduled row's ENT-08 warm-up. An ad-hoc fire's row is transient
            (never persisted to `entry_schedule`, no open entry yet), so it is
            NOT in `_wanted()` and its stream is not built by the probe
            cadence -- selection would skip `no_chain_stream:<underlying>`,
            making an ad-hoc RUT fire silently un-firable while ad-hoc SPX
            rides the {SPX} fallback. This provisions the stream on demand,
            just-in-time, so the immediately-following floor guard + selection
            can read the row's own chain.

            Idempotent: a stream already present (built by the probe, or a
            prior ensure this same fire) is a NO-OP -- never torn down or
            disturbed. Unknown/disabled profile -> no-op (the downstream
            mismatch / no_chain_stream skip still fail-closes). The prime is
            wrapped in the SAME per-stream try/except as `take()` (FIX-8): a
            provision failure marks THAT stream stale and warns, and NEVER
            raises into the fire path (it fail-closes to a named skip).

            FIX-11: a short-lived PIN (`self._pinned[name]`, `_pin_ttl_s`
            ahead) is set BEFORE the stream is created/primed, and `_wanted()`
            unions the live pins -- so a concurrent ~60s health-tick `sync()`
            during the (1-20s network) prime can never prune this stream
            mid-flight. The pin lapses after the fire window; an unfilled
            ad-hoc stream is then pruned normally, while a FILLED one is
            already wanted via the open-entry set (its survival no longer
            depends on the pin). An ad-hoc stream that never fills is pruned
            once its pin expires; one whose entry FILLS becomes wanted via the
            open-entry set and survives."""
            from meic.domain.underlying import PROFILES as _PROFILES
            from meic.domain.underlying import profile_for as _profile_for

            profile = _profile_for(underlying)
            if profile is None or not profile.enabled:
                return  # unknown/disabled: downstream fail-closes, nothing to pin/provision
            name = profile.name
            # FIX-11: pin BEFORE creating/priming (and refresh on re-ensure),
            # so a sync() racing the prime keeps the stream via the union.
            self._pinned[name] = self._now() + self._pin_ttl_s
            if name in self._streams:
                return  # idempotent -- never disturb an existing (e.g. SPX) stream
            stream = _SnapshotStream(_PROFILES[name])
            self._streams[name] = stream
            try:
                await stream.take()
            except Exception as exc:  # noqa: BLE001 -- FIX-8 shape: never raise into the fire path
                # DROP the just-created transient stream (do NOT keep a stale
                # one) AND clear the pin we just set (FIX-11: never leave a pin
                # pointing at a dropped stream). Rationale: the selector
                # re-invokes `stream.take()` at fire time via `provider_for` --
                # a kept-but-failing stream would re-raise `snapshot_chain`'s
                # error straight into the fire path (the selector does not
                # catch snapshot-acquisition errors). Dropping makes
                # `provider_for` return None instead, so the fire fail-closes
                # to the clean `no_chain_stream:<u>` skip -- "never raises into
                # the fire path". Unlike FIX-8's `take()` (which marks a WANTED
                # stream stale and keeps it), this stream is TRANSIENT/unwanted,
                # so dropping is the honest fail-close, not a regression of the
                # aggregate .stale gate.
                self._streams.pop(name, None)
                self._pinned.pop(name, None)
                comp.alerts.alert(
                    "warning",
                    f"ad-hoc chain provision failed for {name} "
                    f"(fire will fail-close to no_chain_stream:{name}): {exc!r}")

        def for_underlying(self, name: str) -> "_SnapshotStream | None":
            """FIX-7: a PLAIN dict lookup against the current stream map --
            never a sync/fold. None when this underlying's stream isn't built
            yet (raced ahead of the first probe): the caller fail-closes."""
            return self._streams.get(name)

        def snapshot_for(self, name: str):
            """The `_snap_for` seam: this underlying's held snapshot, or None
            (honest absence — never another underlying's chain). FIX-7: plain
            lookup, no fold."""
            stream = self._streams.get(name)
            return None if stream is None else stream.last

        def provider_for(self, name: str):
            """The selector's `snapshot_router` (UND-01): a zero-arg awaitable
            taking a FRESH snapshot for `name`'s stream, or None when no
            stream exists — the selector then skips `no_chain_stream:<name>`,
            never falling back to another underlying's chain. FIX-7: plain
            lookup, no fold."""
            stream = self._streams.get(name)
            return None if stream is None else stream.take

        async def take(self):
            """The health-tick/pre-flight data probe: reconcile the stream set
            (FIX-7: the ONE sync per probe) then refresh EVERY wanted stream.
            Returns the default (SPX, else sole) stream's snapshot so legacy
            callers keep their single-snapshot return shape.

            FIX-8 (cross-underlying outage isolation, 2026-07-21): each
            stream's refresh is wrapped independently -- a transient RUTW
            snapshot failure marks ONLY the RUT stream stale and alerts
            (RSK-06 warning, naming the underlying), then the loop continues
            so SPX still refreshes. A RUT-only data problem must never take
            SPX down (which, via the conservative any-stream-stale aggregate,
            would DAT-02-block every entry)."""
            self.sync()
            result = None
            for name, stream in list(self._streams.items()):
                try:
                    snap = await stream.take()
                except Exception as exc:  # noqa: BLE001 -- one stream's failure never aborts the rest
                    stream.stale = True   # fail-closed for THIS underlying only (DAT-02)
                    comp.alerts.alert(
                        "warning",
                        f"chain snapshot refresh failed for {name} "
                        f"(underlying isolated -- other underlyings unaffected): {exc!r}")
                    continue
                if name == "SPX" or result is None:
                    result = snap
            return result

        def _default_stream(self) -> "_SnapshotStream":
            from meic.domain.underlying import PROFILES as _PROFILES

            existing = self._streams.get("SPX")
            if existing is not None:
                return existing
            if self._streams:
                return next(iter(self._streams.values()))
            return self._streams.setdefault("SPX", _SnapshotStream(_PROFILES["SPX"]))

        def fresh_for(self, underlying: str) -> bool:
            """UND-05 (v1.86 loosening, operator ruling 2026-07-21): PER-
            UNDERLYING freshness for the OUTER ENT-03 data_fresh gate -- a
            RUT-only data outage must not block SPX entries (the selector's
            own `_attempt` already fail-closes per-underlying; this closes
            the last aggregate surface). Fail-closed: a stream that does not
            exist yet (not built by the probe cadence, or an unknown/
            disabled underlying) reads NOT fresh, exactly like a stale one
            -- never True for an absent stream. FIX-7: plain dict lookup, no
            fold/sync."""
            stream = self._streams.get(underlying)
            return stream is not None and not stream.stale

        # Legacy surface — the DAT-02 gate reads `.stale` (any wanted stream
        # stale = not fresh: pause, never guess); FEATURE 3 and the wiring
        # tests read/STUB `.last`. Setters write through to the default
        # stream so `snaps.last = fake` / `snaps.stale = False` (the existing
        # test idiom) keeps working unchanged.
        @property
        def stale(self) -> bool:
            streams = list(self._streams.values())
            if not streams:
                return True
            return any(s.stale for s in streams)

        @stale.setter
        def stale(self, value: bool) -> None:
            self._default_stream().stale = bool(value)

        @property
        def last(self):
            stream = self._streams.get("SPX") or next(iter(self._streams.values()), None)
            return None if stream is None else stream.last

        @last.setter
        def last(self, snap) -> None:
            self._default_stream().last = snap

    snaps = _Snapshots()

    async def _data_fresh(underlying: str = "SPX") -> bool:
        """DAT-02/UND-05 (v1.86 loosening, operator ruling 2026-07-21): the
        ENT-03 data_fresh gate now resolves PER the attempt's own
        underlying -- a stale/absent RUT stream blocks only RUT entries,
        never SPX's. `underlying` defaults to "SPX" so every bare/legacy
        caller (paper, pre-v1.86 tests, `LiveMarketGates.__call__`'s own
        default, the UC-02 preflight's separate `not snaps.stale` binding
        just below) reads byte-identical to the old aggregate check in the
        single-underlying case."""
        return snaps.fresh_for(underlying)

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
        # UND-01/UND-04 (v1.86): per-underlying routing -- each attempt
        # resolves the ROW's own underlying's stream; no stream => skip
        # `no_chain_stream:<underlying>`, never another underlying's chain.
        snapshot_router=snaps.provider_for,
        # STK-10: the chain-scoped completeness dial (doc 06, 50-100, default 90),
        # previously hardcoded at 90 inside SelectionConfig.
        config=SelectionConfig(completeness_pct=_chain_completeness_pct(env),
                               min_validated_strikes=_min_validated_strikes(env)),
        # UND-02 (v1.86) PRECEDENCE: the env dial, when SET, is the operator
        # override for ALL underlyings (baked into `config` above, flag off);
        # UNSET, the gate resolves each ROW's underlying-profile default
        # (identical numbers for SPX/RUT this phase -- byte-identical today).
        completeness_from_profile="MEIC_CHAIN_COMPLETENESS_PCT" not in env,
        min_validated_from_profile="MEIC_MIN_VALIDATED_STRIKES" not in env,
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
    # RSK-01a (v1.68 constant-signal fix, NFR-07's pinned regression): this
    # used to fall through to LiveMarketGates' dead `lambda: False` default --
    # present, called, green forever, never actually reading whether a
    # Flatten All is executing. `flatten_in_progress` is now the REQUIRED
    # caller-supplied live signal (see this function's docstring).
    # DAT-04a v1.80: no more `halted=` kwarg — the input is retired, never
    # stubbed (see composition/live_gates.py's module docstring).
    gates = LiveMarketGates.for_live(clock=comp.clock, data_fresh=_data_fresh,
                                     session_valid=_session_valid, buying_power_ok=_buying_power_ok,
                                     flatten_in_progress=flatten_in_progress,
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
            # UND-01/UND-04 (v1.86): lock the baseline from THIS ROW's own
            # underlying's snapshot (take() above refreshed every wanted
            # stream, this row's included) -- never another underlying's
            # chain. Default SPX matches every pre-v1.86 config.
            warm_underlying = getattr(config, "underlying", "SPX") if config is not None else "SPX"
            selector.warm_baseline(snaps.snapshot_for(warm_underlying), config,
                                   when=when, entry_number=entry_number)

        cap_seconds = max(0.0, lead_seconds - ALERT_AT_SECONDS)
        completed, _ = await run_warmup(_prime(), cap_seconds=cap_seconds)
        if not completed:
            comp.alerts.alert(
                "critical",
                f"ENT-08 warm-up capped at T-{ALERT_AT_SECONDS:.0f}s for entry "
                f"#{entry_number} at {when.isoformat()} -- firing on schedule regardless")

    # RSK-04 + RSK-08 + ENT-03 BP, all armed. Also wraps comp.broker so the order
    # cap counts every order any service submits. ENT-05 v1.81 (RETIRED): no
    # entry-count cap is threaded through here anymore.
    runtime = build_live_runtime(comp, selector=selector, market_gates=gates,
                                 warmup=_entry_warmup, warmup_lead_seconds=lead_seconds,
                                 drift=drift, max_clock_drift_ms=max_drift_ms,
                                 calendar_label=calendar_store.label_for_day)  # CAL-05

    # ENT-09: the panel's ▶ crosses the identical rails (same ceiling, same book).
    manual = build_manual_entry(
        comp, selector=selector, market_gates=gates,
        drift=drift, max_clock_drift_ms=max_drift_ms,
        calendar_label=calendar_store.label_for_day,  # CAL-06
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
        # UND-01 (v1.86): accepts an optional underlying name so a caller
        # that knows its row's underlying gets THAT spot; the zero-arg call
        # (every pre-v1.86 caller inside build_manual_entry) stays the SPX
        # default, byte-identical.
        spot_provider=lambda underlying="SPX": getattr(
            snaps.snapshot_for(underlying), "spot", None),
        # FIX-10 (v1.86): provision the row's own underlying's chain stream
        # just-in-time before an ad-hoc (ENT-11) fire selects -- the ad-hoc
        # analog of the scheduled row's ENT-08 warm-up. Idempotent no-op for
        # a stream the probe already built (every armed/open underlying,
        # incl. the schedule ▶ lane). Fail-closed, never raises into fire().
        ensure_underlying=snaps.ensure)

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

        # UND-01/UND-04 (v1.86): THIS long's own underlying's snapshot,
        # resolved from its OCC root -- a RUTW long is never priced (or
        # intrinsic-floored) off the SPX chain/spot; no stream => defer
        # honestly, exactly like "no snapshot yet".
        snap = snaps.snapshot_for(_underlying_of_symbol(long_symbol))
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

    # EC-STP-06 (2026-07-14): the age cutoff bounding the catch-up below to
    # RECENT entries only -- see `_stop_fill_catchup_max_age_days`'s docstring
    # for the incident this closes (an already-resolved, days-old entry
    # re-diffed against today's broker truth on every boot, forever).
    stop_fill_catchup_max_age_days = _stop_fill_catchup_max_age_days(env)

    async def _detect_stop_fills() -> None:
        """EC-STP-06 (v1.60): catch up any stop fill this process missed
        while it was UP and running — the exact gap behind the 2026-07-10
        11:56 incident (the C7565 CALL stop filled at 11:56:15 ET and nothing
        noticed: no SIDE_STOPPED, no LEX, no UI feedback). Run every health
        tick (see `_probe_once`); `comp.alerts` is read at CALL time (not
        closure-construction time) so it resolves to whichever AlertSink
        `live_app()` ends up assigning.

        `today` is read FRESH every call (DAY-03: `trading_day(comp.clock.
        now())`, never a wall-clock date) and passed with
        `stop_fill_catchup_max_age_days` to bound the catch-up to recent
        entries only (2026-07-14) -- see `_stop_fill_catchup_max_age_days`'s
        docstring."""
        from meic.application.stop_fill_watch import detect_and_recover_stop_fills

        today = trading_day(comp.clock.now()).isoformat()
        await detect_and_recover_stop_fills(comp, comp.alerts, _long_quote,
                                            today=today, max_age_days=stop_fill_catchup_max_age_days)

    def _floor_candidates(row) -> dict:
        """ENT-09b v1.57: the ▶ dialog's floor dropdowns. Thin -- the actual
        computation is the pure, independently-tested
        `composition.live_selection.floor_candidates`; this closure only
        supplies the live snapshot and the row's own SelectionConfig."""
        cfg = SelectionConfig.for_entry(row) if row is not None else selector.config
        # UND-01/UND-04 (v1.86): the dialog populates from THE ROW's own
        # underlying's snapshot; no stream for it -> `floor_candidates`'s own
        # None path (empty candidates) -- never another underlying's strikes.
        return floor_candidates_fn(snaps.snapshot_for(getattr(cfg, "underlying", "SPX")), cfg)

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
        # DAT-04a v1.80: the halt-signal store is RETIRED — no longer exposed
        # here (see composition/live_gates.py's module docstring).
        "max_quote_age_ms": max_quote_age_ms,
        # EC-STP-06 (2026-07-14): exposed for the wiring capstone, mirroring
        # `stop_fill_poll_interval_s` -- proves the cutoff actually comes
        # from env, not a hardcoded value.
        "stop_fill_catchup_max_age_days": stop_fill_catchup_max_age_days,
        # CAL-01..08: exposed so live_app can wire /calendar/* (create_app)
        # and so the NFR-07 registry's behavioural live-check can flip the
        # real store off app.state, mirroring quote_hub.
        "calendar_store": calendar_store,
    }



def _quiet_noisy_third_party_loggers(env: dict[str, str]) -> None:
    """Keep the SDK's DEBUG firehose out of the operator's log.

    Measured on the 2026-07-26 deploy: 167 of 221 lines in the first two
    minutes were `DEBUG tastytrade` quote dumps, each carrying a full chain
    snapshot. That is not merely untidy -- it directly weakens NFR-08a. The
    whole point of that rule is that a failing evaluator produces an
    OPERATOR-VISIBLE signal rather than a log line nobody reads, and a CRITICAL
    buried in thousands of quote dumps is closer to the second than the first.
    We would have replaced "log-only, therefore invisible" with "alerted, but
    unfindable".

    Only the THIRD-PARTY feed loggers are raised; `meic.*` is untouched, so
    nothing this codebase says about its own state is suppressed. Overridable
    via MEIC_SDK_LOG_LEVEL for a debugging session."""
    import logging as _logging

    level_name = (env.get("MEIC_SDK_LOG_LEVEL") or "INFO").upper()
    level = getattr(_logging, level_name, _logging.INFO)
    for name in ("tastytrade", "websockets", "httpx", "httpcore"):
        _logging.getLogger(name).setLevel(level)

def live_app():
    """Live composition behind the panel: real broker + feed, SQLite-persisted,
    token-gated, safe defaults. Connects on startup; NO trading auto-starts —
    the operator arms + confirms live deliberately. CERT sandbox by default."""
    from meic.adapters.api.app import create_app
    from meic.adapters.persistence.event_store import SqliteStateStore
    from meic.application.clocks import SystemClock
    from meic.application.lex_ladder_watchdog import LexLadderWatchdog
    from meic.application.watchdog import StopWatchdog
    from meic.composition.live import LiveComposition
    from meic.composition.panel_commands import PanelCommands

    env = _read_env()
    _quiet_noisy_third_party_loggers(env)
    configure_logging(env, root=ROOT)  # 2026-07-14: logging works however the app is started
    is_test = env.get("MEIC_LIVE_IS_TEST", "true").lower() != "false"
    # Boot announcement: which environment, never which secret -- see
    # adapters/logging_setup.py's module docstring on what this module does
    # and does not log.
    logger.info("live_app booting: kind=%s", "CERT" if is_test else "PROD")
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

    # RSK-01a (v1.68, operator-ratified fix for the NFR-07 constant-signal
    # regression): PanelCommands is the real owner of `flatten_in_progress`
    # (true only for the duration of its own `flatten()` call), but it is
    # constructed AFTER `_wire_live_day` below builds the live gates -- see
    # `commands = PanelCommands(...)` further down. Reordering construction
    # would ripple through every other thing `_wire_live_day` builds (the
    # selector, the manual-entry wiring, the pre-flight checks all close over
    # objects built inside it), so instead this late-binding cell is the seam:
    # the gate holds `flatten_signal` (a plain callable) from the start, and
    # `flatten_signal.target` is pointed at the real `commands` once it exists
    # a few lines down. Every gate evaluation reads `.target` at CALL time
    # (never cached), so this is a live signal, not a snapshot taken here.
    class _FlattenSignalCell:
        target: object | None = None

        def __call__(self) -> bool:
            return bool(self.target is not None and self.target.flatten_in_progress)

    flatten_signal = _FlattenSignalCell()

    # The live day, assembled with EVERY safety rail armed. Built BEFORE create_app
    # so the panel's ▶ button and pre-flight get the real thing, not stubs. See
    # composition/live_wiring.py — and tests/composition/test_live_wiring.py, which
    # asserts on those functions precisely because this is where rails go missing.
    live = _wire_live_day(comp, env, flatten_in_progress=flatten_signal)

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

        shorts: list[OpenShortMark] = []
        for _entry_id, side, leg, spec in _open_short_legs(comp.events):
            mark = None
            # UND-01/UND-04 (v1.86): each short marks off ITS OWN underlying's
            # snapshot (routed by OCC root); no stream -> honest None.
            snap = _snap_for(live["snapshots"], _underlying_of_symbol(leg.symbol))
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
    # TPF-03a: the dedicated exit-evaluation loop's cadence (doc 06,
    # 100-5000 ms, default 250). Read here so it is bound ONCE for the
    # process, alongside the freshness bar it works with.
    exit_eval_interval_ms = _exit_eval_interval_ms(env)
    # NFR-08a / TPF-03d: the alert cooldown AND the unevaluable-for-this-long
    # threshold are the same ratified dial. Bound onto the composition so the
    # limiter built on first failure uses the OPERATOR's value, not the
    # import-time default.
    comp.exit_unevaluable_alert_s = _exit_unevaluable_alert_s(env)

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
    # RSK-01a: bind the late-binding cell above to the now-real `commands` --
    # every entry-gate evaluation from this point on reads
    # `commands.flatten_in_progress` LIVE (never a constant, never cached).
    flatten_signal.target = commands
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
                     backfill_broker_reads=_BrokerReadFacade(comp.broker),
                     calendar_store=live["calendar_store"],   # CAL-01..08
                     cal_stale_after_days=_cal_stale_after_days(env),
                     event_warning_lead_days=_event_warning_lead_days(env))   # CAL-11
    app.state.composition = comp
    app.state.commands = commands
    # CAL-01..08: exposed like every other live signal above so a test/the
    # NFR-07 registry's behavioural live-check can flip the real store.
    app.state.calendar_store = live["calendar_store"]
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
    # DAT-04a v1.80: the halt-signal store is RETIRED — no longer exposed here.
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

    # CAL-09 (v1.77): the daily official-source auto-refresh coordinator.
    # Constructed UNCONDITIONALLY (mirroring `calendar_store` above) so the
    # NFR-07 registry can always prove it exists -- `cal_auto_refresh`
    # (below) gates whether the daily tick actually INVOKES it, never
    # whether it is built. Read-only, unauthenticated, named-domains-only
    # fetch adapters (adapters/calendar_sources/*); every outcome journals
    # through the SAME `calendar_store` CAL-01..08 already shares.
    from meic.adapters.calendar_sources import BeaSource, BlsSource, FomcSource
    from meic.application.calendar_refresh import CalendarRefreshCoordinator

    calendar_refresh_coordinator = CalendarRefreshCoordinator(
        sources=(FomcSource(), BlsSource(), BeaSource()),
        store=live["calendar_store"], clock=comp.clock, alerts=alerts,
        fail_alert_days=_cal_refresh_fail_alert_days(env))
    cal_auto_refresh = _cal_auto_refresh(env)
    # Exposed like `calendar_store` above so a test/the NFR-07 registry can
    # reach the real coordinator off app.state.
    app.state.calendar_refresh_coordinator = calendar_refresh_coordinator
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
            broker=comp.broker, events=comp.events, state=comp.state, alerts=alerts,
            clock=comp.clock)
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
            logger.warning("health tick: session_probe failed: %r", exc)
        try:
            await live["data_probe"]()
        except Exception as exc:  # noqa: BLE001
            app.state.broker_error = repr(exc)
            logger.warning("health tick: data_probe failed: %r", exc)
        try:
            # RPT-12/D8: sample marks off the snapshot just refreshed above,
            # same cadence, independent of either probe's success (the
            # sampler itself degrades to a no-op on a missing/stale snapshot).
            # UND-01/UND-04 (v1.86): the HOLDER itself, so each entry samples
            # off its OWN underlying's stream (`_snap_for` inside).
            _sample_marks_once(comp, live["snapshots"])
        except Exception as exc:  # noqa: BLE001
            app.state.broker_error = repr(exc)
            logger.warning("health tick: mark sampling failed: %r", exc)
        # TPF-03a (v1.94): exit evaluation is NO LONGER A DUTY OF THIS TICK.
        # It was, and that was the defect of record (2026-07-26): this tick is
        # SLEEP-FIRST on a 60 s interval, so every boot went a full minute
        # before the first evaluation, a breach beginning and ending inside one
        # window was NEVER OBSERVED, and a persisting breach acted 60-120 s
        # late. TPF-03 already required "every valid quote evaluation"; the
        # code did not do what was ratified.
        #
        # It now runs on its OWN loop whose ONLY duty is exit evaluation
        # (`_start_exit_eval_loop` below, `exit_eval_interval_ms`, default
        # 250 ms), evaluate-FIRST and skip-if-busy. Exactly the EC-STP-06
        # precedent immediately below: one owner per concern. Do NOT add an
        # exit-evaluation call back onto this tick -- two owners at different
        # cadences is worse than one at the wrong cadence.
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
            logger.warning("health tick: EOD sweep failed: %r", exc)
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
            logger.warning("health tick: EOD reconcile failed: %r", exc)
        try:
            # CAL-09 (v1.77): rides the SAME daily-self-init cadence as
            # EOD-03/RPT-15 above -- `should_run` is once-per-ET-trading-day,
            # plus a stale-boot catch-up (this tick already runs once at
            # boot via `_connect`'s explicit `_probe_once()` call, so no
            # separate boot hook is needed). `cal_auto_refresh=False` is the
            # operator's opt-out (manual paste import keeps working
            # regardless); a fetch/parse failure here is ALREADY handled
            # inside the coordinator (reject-don't-replace, alerted) -- this
            # try/except only guards the tick loop itself from a coordinator
            # bug, mirroring every other duty on this tick.
            if cal_auto_refresh:
                now = datetime.now(ET)
                if calendar_refresh_coordinator.should_run(now):
                    await calendar_refresh_coordinator.run_once(now)
        except Exception as exc:  # noqa: BLE001
            app.state.broker_error = repr(exc)
            logger.warning("health tick: CAL-09 calendar refresh failed: %r", exc)

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
            # UND-01/UND-04 (v1.86): the holder, routed per entry inside.
            await _recover_exits_once(comp, live["snapshots"], commands,
                                      hub=hub, clock=comp.clock, max_quote_age_ms=max_quote_age_ms)
        except Exception as exc:  # noqa: BLE001 — surfaced, never fatal
            app.state.broker_error = repr(exc)
            # PRE-EXISTING RISK, flagged not fixed here (out of this item's
            # scope): `repr(exc)` on a `comp.connect()` failure is already
            # returned verbatim in the `/broker/connect` JSON response today;
            # if the underlying SDK ever embeds the secret/refresh token/
            # session credential in an auth-failure exception's own message,
            # that string would appear here too. No broker/adapter exception
            # message in this codebase is known to do that today, but this
            # logger call does not increase exposure beyond what the API
            # response already discloses -- it only makes it durable.
            logger.error("boot connect failed: %r", exc)

    # NFR-02 + DAY-03: the periodic health loop the gates and pre-flight assume
    # exists. It keeps the session-liveness and broker-clock reading FRESH (a
    # reading older than 300 s is treated as unverified). Without it the clock is
    # never verified and the arm pre-flight blocks forever. Runs on the main event
    # loop — the SAME loop comp.connect bound the broker session to — so awaiting
    # broker calls here is safe (unlike the threadpool pre-flight route).
    health_interval_s = float(env.get("MEIC_HEALTH_INTERVAL_S", "60"))
    app.state.health_loop_error = None   # last tick failure, repr -- None when healthy

    @app.on_event("startup")
    async def _start_health_loop() -> None:
        async def _tick() -> None:
            if app.state.broker_connected:
                await _probe_once()

        async def _loop() -> None:
            while True:
                await asyncio.sleep(health_interval_s)
                # v1.74 health-loop guard: a per-tick exception must never
                # kill this loop (RSK-06) -- see `_health_tick`'s docstring.
                await _health_tick(app.state, alerts, _tick)
        app.state.health_task = asyncio.create_task(_loop())
        # v1.74 health-loop guard: alert CRITICAL if the task itself ever
        # dies (see `_health_task_done_callback`'s docstring).
        app.state.health_task.add_done_callback(_health_task_done_callback(alerts))

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
    async def _start_exit_eval_loop() -> None:
        """TPF-03a: the DEDICATED exit-evaluation owner. Its only duty is
        evaluating floors and targets.

        Three properties the health tick did not have, each one a defect it
        actually exhibited:

          * EVALUATE FIRST, then sleep. Sleep-first meant every boot ran blind
            for a full interval -- 60 s with an armed floor, at the one moment
            (just after a restart) when state is least certain.
          * SKIP IF BUSY, never queue. At 250 ms a pass that overruns must not
            let passes pile up behind it; a lock that is already held means an
            evaluation is in flight, which is the thing we wanted anyway.
          * The loop CANNOT DIE. A per-pass exception is caught, surfaced and
            retried on the next pass, and the task carries the same
            done-callback the health loop uses so a loop that dies anyway is a
            CRITICAL alert rather than silence. An exit evaluator that stops
            evaluating looks exactly like an entry that never breached.

        TPF-03g's per-entry isolation lives INSIDE `_evaluate_exits_once`, not
        here: this guard keeps the LOOP alive, which is a strictly weaker
        guarantee than keeping the PASS complete."""
        lock = asyncio.Lock()
        app.state.exit_eval_lock = lock
        app.state.exit_eval_error = None
        app.state.exit_eval_passes = 0
        interval_s = exit_eval_interval_ms / 1000.0

        async def _pass() -> None:
            if not app.state.broker_connected:
                return
            if lock.locked():
                return          # a pass is already in flight -- skip, never queue
            async with lock:
                await _evaluate_exits_once(
                    comp, live["snapshots"], live["exit_monitor"], commands,
                    hub=hub, clock=comp.clock, max_quote_age_ms=max_quote_age_ms)
                app.state.exit_eval_passes += 1

        async def _loop() -> None:
            while True:
                try:
                    await _pass()          # EVALUATE FIRST -- never sleep-first
                    app.state.exit_eval_error = None
                except Exception as exc:  # noqa: BLE001 -- must never kill this loop
                    app.state.exit_eval_error = repr(exc)
                    app.state.broker_error = repr(exc)
                    # NFR-08a: a raised exception from the pass ALERTS. The
                    # predecessor logged a warning and nothing else, so a
                    # throwing evaluator left every exit dead for a session
                    # with no operator-visible signal. TPF-03g isolates the
                    # per-ENTRY failures inside the pass; this catches a
                    # failure of the pass ITSELF (before or around the loop),
                    # which would blind every entry at once.
                    _alert_exit_failure(comp, exc, now=comp.clock.now())
                await asyncio.sleep(interval_s)

        app.state.exit_eval_task = asyncio.create_task(_loop())
        app.state.exit_eval_task.add_done_callback(_health_task_done_callback(alerts))

    @app.on_event("shutdown")
    async def _stop_exit_eval_loop() -> None:
        task = getattr(app.state, "exit_eval_task", None)
        if task:
            task.cancel()

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
                        logger.warning("stop-fill poll fallback failed: %r", exc)
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
                                 fee_model=comp.fee_model, clock=comp.clock)
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

    # LEX-07 (2026-07-14): the LEX-ladder invariant watchdog -- the CLASS fix
    # for the 2026-07-10 incident (`ShortStopped` journaled, then NOTHING: no
    # `LongSaleStarted`, the LEX ladder had silently never run, and the log
    # itself was the only place that ever showed it -- no wrong events, only
    # absent ones). Constructed and ticked unconditionally here, same as the
    # stop watchdog above -- an unwired detector would be the very bug this
    # closes. Purely journal-driven (application/lex_ladder_watchdog.py): it
    # never touches the broker, the hub, or the LEX service, so it fires even
    # when all three are unwired, unreachable, or dead.
    lex_ladder_grace_s = _lex_ladder_watchdog_grace_seconds(env)
    app.state.lex_ladder_watchdog_grace_seconds = lex_ladder_grace_s
    lex_ladder_watchdog = LexLadderWatchdog(alerts=alerts, grace_seconds=lex_ladder_grace_s)
    app.state.lex_ladder_watchdog = lex_ladder_watchdog

    @app.on_event("startup")
    async def _start_lex_ladder_watchdog_loop() -> None:
        app.state.lex_ladder_watchdog_task = asyncio.create_task(_run_lex_ladder_watchdog_loop(
            comp, lex_ladder_watchdog, alerts, clock=comp.clock, idle_seconds=quote_stream_poll_s))

    @app.on_event("shutdown")
    async def _stop_lex_ladder_watchdog_loop() -> None:
        task = getattr(app.state, "lex_ladder_watchdog_task", None)
        if task:
            task.cancel()

    # UND-03/F3 (v1.86 /ES Stage 2, 2026-07-21): the force-close scheduler --
    # /ES is NEVER held to settlement (American exercise would assign a
    # futures position, breaking the cash-settlement/defined-risk contract
    # EOD-01 relies on). Constructed and ticked UNCONDITIONALLY, same shape
    # as every other watcher above -- the policy table
    # (`force_close_scheduler.default_policies`) is what makes it inert on an
    # SPX/RUT-only day: only underlyings with `mandatory_eod_close` (today:
    # /ES only) get an entry, so `run_once` finds nothing to do unless a /ES
    # entry actually exists. Purely journal + `comp.close`/`comp.broker` --
    # same close path every other close in this codebase uses (CLS-01/02),
    # never a second implementation.
    from meic.application.force_close_scheduler import ForceCloseScheduler, default_policies

    es_eod_close_deadline = _es_eod_close_deadline(env)
    app.state.es_eod_close_deadline = es_eod_close_deadline
    # FIX 2 (half-day): the SAME algorithmic early-close calendar the EOD-03
    # sweep already computed above (`eod_half_days`, a 10-year window) so a
    # 13:00-ET half day force-closes /ES before 13:00, not at 15:55.
    force_close_scheduler = ForceCloseScheduler(
        comp, policies=default_policies(eod_close_deadline=es_eod_close_deadline),
        half_days=eod_half_days)
    app.state.force_close_scheduler = force_close_scheduler

    @app.on_event("startup")
    async def _start_force_close_scheduler_loop() -> None:
        app.state.force_close_scheduler_task = asyncio.create_task(_run_force_close_scheduler_loop(
            comp, force_close_scheduler, alerts, clock=comp.clock, idle_seconds=quote_stream_poll_s))
        # SETTLEMENT-SAFETY (2026-07-21 final review): alert CRITICAL if the
        # force-close task itself ever dies -- the same death detection
        # health_task has (see `_force_close_task_done_callback`). Without it
        # a dead force-close loop rides every open /ES into settlement
        # silently, the exact F3 outcome this whole component prevents.
        app.state.force_close_scheduler_task.add_done_callback(
            _force_close_task_done_callback(alerts))

    @app.on_event("shutdown")
    async def _stop_force_close_scheduler_loop() -> None:
        task = getattr(app.state, "force_close_scheduler_task", None)
        if task:
            task.cancel()

    # DCY-01..04 (2026-07-14, NFR-07 regression): the decay buyback watcher --
    # `DecayWatcher` (application/decay_watcher.py) was fully written, unit-
    # tested (test_tpf_dcy.py) and race-guarded (test_decay_watcher_live_shaped.py,
    # whose own docstring flagged it as unwired) but never constructed anywhere
    # outside its own tests -- grep confirmed zero `DecayWatcher(` hits under
    # backend/src. Fed the SAME live QuoteHub every other watcher above reads,
    # translated through the SAME `_streamer_symbol` seam (NFR-04: a decay
    # decision must never fire off a stale snapshot). Constructed and ticked
    # unconditionally here, same shape as the stop watchdog above.
    #
    # `app.state.decay_watcher` is a single always-present instance exposed for
    # the wiring-audit registry's "constructed" proof and for introspecting the
    # resolved config; the pass loop's actual per-tracked-short instances (one
    # per (entry_id, side), never shared -- `evaluate()`'s confirmation counter
    # is a single scalar) live in `app.state.decay_watchers`, populated lazily
    # as tracked shorts appear -- see `_decay_watcher_pass`'s own docstring.
    decay_buyback_enabled = _decay_buyback_enabled(env)
    decay_buyback_trigger = _decay_buyback_trigger(env)
    decay_confirmation_evals = _decay_confirmation_evals(env)
    decay_unfilled_timeout_seconds = _decay_unfilled_timeout_seconds(env)
    decay_cutoff_time = _decay_cutoff_time(env)
    app.state.decay_buyback_enabled = decay_buyback_enabled
    app.state.decay_buyback_trigger = decay_buyback_trigger
    app.state.decay_confirmation_evals = decay_confirmation_evals
    app.state.decay_unfilled_timeout_seconds = decay_unfilled_timeout_seconds
    app.state.decay_cutoff_time = decay_cutoff_time
    from meic.application.decay_watcher import DecayWatcher as _DecayWatcher

    app.state.decay_watcher = _DecayWatcher(
        broker=comp.broker, events=comp.events, decay_buyback_trigger=decay_buyback_trigger,
        decay_confirmation_evals=decay_confirmation_evals, fee_model=comp.fee_model,
        clock=comp.clock)
    # (entry_id, side) -> DecayWatcher / in-flight-buyback info -- the REAL
    # bookkeeping the pass loop reads and mutates every tick (passed INTO
    # `_run_decay_watcher_loop` below, never a private copy the loop keeps to
    # itself) -- so the wiring registry and tests can observe actual ticking,
    # not just a decorative object nobody reads.
    app.state.decay_watchers = {}
    app.state.decay_watcher_active = {}

    @app.on_event("startup")
    async def _start_decay_watcher_loop() -> None:
        app.state.decay_watcher_task = asyncio.create_task(_run_decay_watcher_loop(
            comp, hub, live["snapshots"], alerts, clock=comp.clock,
            max_quote_age_ms=max_quote_age_ms, buyback_trigger=decay_buyback_trigger,
            confirmation_evals=decay_confirmation_evals,
            unfilled_timeout_seconds=decay_unfilled_timeout_seconds,
            cutoff_time=decay_cutoff_time, enabled=decay_buyback_enabled,
            fee_model=comp.fee_model, flatten_in_progress=lambda: commands.flatten_in_progress,
            idle_seconds=quote_stream_poll_s, connected=lambda: app.state.broker_connected,
            watchers=app.state.decay_watchers, active=app.state.decay_watcher_active))

    @app.on_event("shutdown")
    async def _stop_decay_watcher_loop() -> None:
        task = getattr(app.state, "decay_watcher_task", None)
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
            # UND-01/UND-04 (v1.86): the holder, routed per entry inside.
            await _recover_exits_once(comp, live["snapshots"], commands,
                                      hub=hub, clock=comp.clock, max_quote_age_ms=max_quote_age_ms)
        except Exception as exc:  # noqa: BLE001
            app.state.broker_connected = False
            app.state.broker_error = repr(exc)
            # Same pre-existing risk noted on the boot _connect() handler
            # above: `repr(exc)` is already returned verbatim in this
            # endpoint's own JSON response today; this logger call makes an
            # already-disclosed string durable, not newly exposed.
            logger.error("/broker/connect retry failed: %r", exc)
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
