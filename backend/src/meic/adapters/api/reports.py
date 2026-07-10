"""RPT-10: read-only `/reports/*` API — GETs are origin-open exactly like the
existing read model (`/state`, `/report`, `/entries`), panel security
unchanged. Every payload is server-computed over the composition's OWN
event log (never a mock/demo source), carries `mode` and the UI-25 trust
block, and renders Decimals as strings with ET-native date/timestamp fields
(the bot's own `date`/`at` values are already ET — see DAY-03).

This module holds no broker reference at all: it only READS the events list
handed to it at construction (doc 10 Principle 1) and the pure `meic.reporting`
package. RPT-15's broker fetch lives entirely in
`application/report_reconciler.py`, wired separately by server.py; nothing
here can reach it.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Response

from meic.adapters.api.app import _card_legs, _premium_received
from meic.domain.events import (
    CorrectionRecord,
    EntryMarkSample,
    Event,
    ReconciliationMismatch,
    ShortStopped,
    SideUnprotected,
    WatchdogEscalated,
)
from meic.domain.projection import fold
from meic.reporting import slippage as slippage_mod
from meic.reporting.corrections import corrections_for_day
from meic.reporting.folds import (
    contracts_of,
    core_results,
    entries_by_day,
    entry_credit_dollars,
    entry_dollars,
    entry_dollars_fees,
    trading_days,
)
from meic.reporting.metrics import (
    avg_loss,
    avg_win,
    day_win_rate,
    expectancy,
    longest_losing_streak,
    max_drawdown,
    profit_factor,
    roc,
    sharpe,
    sortino,
)
from meic.reporting.periods import resolve_period, scope_events
from meic.reporting.taxonomy import classify, contract_audit
from meic.reporting.trust import trust_stamp
from meic.reporting.waterfall import WaterfallResidualError, build_waterfall

# The event types RPT-12's timeline renders as markers, and their icon
# (doc 10 RPT-12). Not every one of these carries its own wall-clock `at`
# field in the current event schema (only CondorFilled/EntryMarkSample do) --
# a marker whose event has none renders "at": null, a known limitation noted
# in the slice-2 handoff rather than a fabricated timestamp.
_MARKER_ICON = {
    "CondorFilled": "▲",       # entry ▲
    "ShortStopped": "✖",       # stop ✖
    "EntryClosed": "●",        # close ●
    "WatchdogEscalated": "⚡",  # watchdog ⚡
    "SideUnprotected": "▓",    # UNPROTECTED shaded
}

STOP_TICK = Decimal("0.05")  # SPX tick (adapters/api/server.py's SPX TickTable)


@dataclass(frozen=True)
class ReportingConfig:
    """RPT-04 config (doc 06): capital base is REQUIRED for return metrics --
    absent, they render "unconfigured", never a fake denominator."""

    capital_base: Decimal | None
    rf_pct: Decimal = Decimal("0")
    min_sample_days: int = 20
    # RPT-03 contract audit's reference pct, as a FRACTION (e.g. Decimal("0.95")).
    # The event log does not yet carry each entry's OWN stop_loss_pct at fill
    # time (a known slice-2 gap -- see _summary_taxonomy), so callers that want
    # a LIVE value (the schedule's current setting, which can change) pass a
    # zero-arg callable here instead of a fixed Decimal; None disables the audit
    # entirely rather than guessing.
    stop_loss_pct: Decimal | Callable[[], Decimal | None] | None = None


def _resolved_stop_loss_pct(cfg: "ReportingConfig") -> Decimal | None:
    v = cfg.stop_loss_pct
    return v() if callable(v) else v


def _s(d: Decimal | None) -> str | None:
    return None if d is None else str(d)


def _filled_entries(scoped: list[Event]):
    return [e for es in entries_by_day(scoped).values() for e in es if e.net_credit != 0]


def _summary_core(scoped: list[Event]) -> dict[str, Any]:
    r = core_results(scoped)
    return {
        "net_pnl": str(r.net_pnl), "gross_pnl": str(r.gross_pnl), "fees": str(r.fees),
        "filled": r.filled, "fired": r.fired, "skipped_by_reason": r.skipped_by_reason,
        "total_credit": str(r.total_credit),
        "day_win_rate": _s(r.day_win_rate), "entry_win_rate": _s(r.entry_win_rate),
        "premium_capture": _s(r.premium_capture),
    }


def _summary_metrics(scoped: list[Event], cfg: ReportingConfig) -> dict[str, Any]:
    from meic.reporting.folds import daily_net

    if cfg.capital_base is None:
        return {"status": "unconfigured"}  # RPT-04/doc-06: required, never a fake denominator
    daily = daily_net(scoped)
    ordered_days = sorted(daily)
    values = [daily[d] for d in ordered_days]
    entry_pnls = [entry_dollars(e) for e in _filled_entries(scoped)]
    base = cfg.capital_base
    mdd_dollars, mdd_pct = max_drawdown(values, base)
    return {
        "status": "ok",
        "roc": str(roc(values, base)) if values else None,
        "sharpe": _s(sharpe(values, base, rf_pct=cfg.rf_pct, min_sample_days=cfg.min_sample_days)),
        "sortino": _s(sortino(values, base, rf_pct=cfg.rf_pct, min_sample_days=cfg.min_sample_days)),
        "max_drawdown_dollars": str(mdd_dollars), "max_drawdown_pct": str(mdd_pct),
        "profit_factor": _s(profit_factor(values)),
        "expectancy_per_entry": _s(expectancy(entry_pnls)),
        "avg_win_day": _s(avg_win(values)), "avg_loss_day": _s(avg_loss(values)),
        "longest_losing_streak_days": longest_losing_streak(values),
        "day_win_rate": _s(day_win_rate(values)),
        "sample_days": len(values), "min_sample_days": cfg.min_sample_days,
    }


def _summary_taxonomy(scoped: list[Event], cfg: ReportingConfig) -> dict[str, Any]:
    pct = _resolved_stop_loss_pct(cfg)
    distribution: dict[str, int] = {}
    breaches: list[dict[str, Any]] = []
    for e in _filled_entries(scoped):
        outcome = classify(e)
        if outcome is None:
            continue
        distribution[outcome] = distribution.get(outcome, 0) + 1
        if pct is not None:
            breach = contract_audit(e, pct=pct)
            if breach is not None:
                breaches.append({
                    "entry_id": breach.entry_id, "outcome": breach.outcome,
                    "realized": str(breach.realized), "floor": str(breach.floor),
                })
    return {"distribution": distribution, "contract_breaches": breaches}


def _summary_health(events: list[Event], scoped: list[Event]) -> dict[str, Any]:
    skip_reasons: dict[str, int] = {}
    for e in scoped:
        if type(e).__name__ == "EntrySkipped":
            skip_reasons[e.reason] = skip_reasons.get(e.reason, 0) + 1
    return {
        "skip_reason_histogram": skip_reasons,
        "watchdog_escalations": sum(1 for e in scoped if isinstance(e, WatchdogEscalated)),
        "unprotected_events": sum(1 for e in scoped if isinstance(e, SideUnprotected)),
        "rsk03_mismatches": sum(1 for e in scoped if isinstance(e, ReconciliationMismatch)),
        "correction_count": sum(1 for e in scoped if isinstance(e, CorrectionRecord)),
        # Not derivable from the replay log in this slice -- ENT-10 crash
        # alerts and ORD-08 retry classification are surfaced live
        # (server.py's alerts feed / /alerts), not journaled as domain events.
        # Deferred, listed rather than fabricated.
        "ent10_crash_alerts": None,
        "ord08_terminal_retries": None,
    }


def _summary_waterfall(scoped: list[Event]) -> dict[str, Any]:
    entries = _filled_entries(scoped)
    credits = sum((entry_credit_dollars(e) for e in entries), Decimal("0"))
    stop_costs = sum((e.stop_fills * Decimal(100) * contracts_of(e) for e in entries), Decimal("0"))
    recoveries = sum((e.recoveries * Decimal(100) * contracts_of(e) for e in entries), Decimal("0"))
    fees = sum((entry_dollars_fees(e) for e in entries), Decimal("0"))
    # `buybacks`/`slippage` are not yet separated from `stop_costs` at the
    # EntryProjection granularity this slice has (that split needs per-fill
    # initiator-tagged pricing beyond CondorFilled/ShortStopped's current
    # fields) -- pinned at $0 here; this reconciles EXACTLY (never a silent
    # residual) because it is the same arithmetic core_results already uses,
    # just not yet decomposed into all of RPT-11's labelled bars. Listed as a
    # follow-up in the slice-2 handoff.
    buybacks = Decimal("0")
    net_slippage = Decimal("0")
    expected_net = core_results(scoped).net_pnl
    try:
        wf = build_waterfall(credits=credits, stop_costs=stop_costs, recoveries=recoveries,
                             buybacks=buybacks, fees=fees, slippage=net_slippage,
                             expected_net=expected_net)
    except WaterfallResidualError as exc:
        return {"error": "residual", "residual": str(exc.residual),
                "expected_net": str(exc.expected_net), "computed_net": str(exc.computed_net)}
    return {
        "credits": str(wf.credits), "stop_costs": str(wf.stop_costs),
        "recoveries": str(wf.recoveries), "buybacks": str(wf.buybacks),
        "fees": str(wf.fees), "slippage": str(wf.slippage), "net": str(wf.net),
        "premium_capture": _s(wf.premium_capture),
    }


def _trust_payload(events: list[Event], days: tuple[str, ...]) -> dict[str, Any]:
    t = trust_stamp(events, days)
    return {"status": t.status, "confirmed_days": t.confirmed_days,
            "total_days": t.total_days, "label": t.label}


def _markers(scoped: list[Event]) -> list[dict[str, Any]]:
    out = []
    for e in scoped:
        name = type(e).__name__
        if name not in _MARKER_ICON:
            continue
        out.append({"type": name, "icon": _MARKER_ICON[name],
                    "entry_id": getattr(e, "entry_id", None), "at": getattr(e, "at", None)})
    return out


def _timeline(scoped: list[Event]) -> dict[str, Any]:
    marks = [e for e in scoped if isinstance(e, EntryMarkSample)]
    return {
        "marks": [{
            "entry_id": m.entry_id, "at": m.at, "spot": _s(m.spot),
            "put_short_mid": _s(m.put_short_mid), "put_long_mid": _s(m.put_long_mid),
            "call_short_mid": _s(m.call_short_mid), "call_long_mid": _s(m.call_long_mid),
        } for m in marks],
        "markers": _markers(scoped),
    }


def _day_slippage_families(scoped: list[Event]) -> dict[str, Any]:
    stop_outs = [e.slippage for e in scoped if isinstance(e, ShortStopped)]
    ticks = [s / STOP_TICK for s in stop_outs]
    return {
        "stop_outs": {
            "mean": _s(slippage_mod.mean(stop_outs)), "p50": _s(slippage_mod.p50(stop_outs)),
            "p90": _s(slippage_mod.p90(stop_outs)), "max": _s(slippage_mod.maximum(stop_outs)),
            "mean_ticks": _s(slippage_mod.mean(ticks)), "n": len(stop_outs),
        },
        # Long recovery / closes / decay-buyback families need per-fill
        # mark-at-stop / target-price capture this slice's event schema does
        # not yet record -- deferred (slice-2 handoff), never fabricated.
        "long_recovery": None, "closes": None, "decay_buybacks": None,
    }


def build_reports_router(
    events: list[Event],
    *,
    mode: Callable[[], str],
    config: ReportingConfig,
    now: Callable[[], str] | None = None,
) -> APIRouter:
    """`events` is the LIVE composition's own durable event log (never a
    mock/demo source, per doc 10 Principle 1) -- callers pass
    `app.state.composition.events` directly. `mode` reads the CURRENT
    trading mode at request time (paper/live never commingle, Principle 3);
    `now` supplies "today" for the `period=today` bucket (ET, DAY-03) --
    defaults to the real ET wall clock.
    """
    router = APIRouter(prefix="/reports", tags=["reports"])

    def _today() -> str:
        if now is not None:
            return now()
        from meic.composition.live_gates import ET
        from datetime import datetime
        return datetime.now(ET).date().isoformat()

    @router.get("/summary")
    def summary(period: str | None = None, day: str | None = None,
                month: str | None = None, year: str | None = None) -> dict[str, Any]:
        days = resolve_period(trading_days(events), period=period, day=day, month=month,
                              year=year, today=_today())
        scoped = scope_events(events, days)
        return {
            "mode": mode(), "period_days": list(days),
            "trust": _trust_payload(events, days),
            "core": _summary_core(scoped),
            "metrics": _summary_metrics(scoped, config),
            "taxonomy": _summary_taxonomy(scoped, config),
            "health": _summary_health(events, scoped),
            "waterfall": _summary_waterfall(scoped),
        }

    @router.get("/day/{iso_date}")
    def report_day(iso_date: str) -> dict[str, Any]:
        scoped = scope_events(events, (iso_date,))
        day_state = fold(scoped)
        if day_state.date is None and not day_state.entries and not day_state.skipped:
            raise HTTPException(status_code=404, detail="no_data_for_day")
        entries_payload = []
        for entry_id, e in sorted(day_state.entries.items()):
            entries_payload.append({
                "entry_id": entry_id, "status": e.status,
                "net_credit": str(e.net_credit), "pnl": str(e.pnl), "fees": str(e.fees),
                "sides_stopped": list(e.sides_stopped), "sides_expired": list(e.sides_expired),
                "close_initiator": e.close_initiator, "outcome": classify(e),
                "legs": _card_legs(e.legs),
                "premium_received": _premium_received(e.legs) if e.legs else {"PUT": None, "CALL": None},
            })
        return {
            "date": iso_date, "mode": mode(),
            "trust": _trust_payload(events, (iso_date,)),
            "entries": entries_payload,
            "skips": [{"entry_number": n, "reason": r} for n, r in day_state.skipped],
            "timeline": _timeline(scoped),
            "slippage": _day_slippage_families(scoped),
            "corrections": [{
                "field": c.field, "bot_value": c.bot_value, "broker_value": c.broker_value,
                "diff": c.diff, "at": c.at,
            } for c in corrections_for_day(events, iso_date)],
        }

    _CSV_TABLES = ("daily", "entries", "corrections")

    @router.get("/csv")
    def export_csv(table: str, period: str | None = None, day: str | None = None,
                   month: str | None = None, year: str | None = None) -> Response:
        if table not in _CSV_TABLES:
            raise HTTPException(status_code=422, detail={"reason": "unknown_table", "table": table,
                                                          "known": list(_CSV_TABLES)})
        days = resolve_period(trading_days(events), period=period, day=day, month=month,
                              year=year, today=_today())
        scoped = scope_events(events, days)
        buf = io.StringIO()
        m = mode()

        if table == "daily":
            from meic.reporting.folds import daily_net

            writer = csv.writer(buf)
            writer.writerow(["date", "mode", "net_pnl", "trust"])
            daily = daily_net(scoped)
            for d in sorted(daily):
                trust = trust_stamp(events, (d,))
                writer.writerow([d, m, str(daily[d]), trust.status])
        elif table == "entries":
            writer = csv.writer(buf)
            writer.writerow(["entry_id", "mode", "status", "outcome", "net_credit", "pnl", "trust"])
            for entry_id, e in sorted(fold(scoped).entries.items()):
                day = entry_id.split("#", 1)[0]
                trust = trust_stamp(events, (day,))
                writer.writerow([entry_id, m, e.status, classify(e) or "", str(e.net_credit),
                                 str(e.pnl), trust.status])
        else:  # "corrections"
            writer = csv.writer(buf)
            writer.writerow(["date", "mode", "field", "bot_value", "broker_value", "diff", "at"])
            for d in days:
                for c in corrections_for_day(events, d):
                    writer.writerow([c.date, m, c.field, c.bot_value, c.broker_value, c.diff, c.at])

        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{table}.csv"'})

    return router
