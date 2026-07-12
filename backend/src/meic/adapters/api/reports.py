"""RPT-10: read-only `/reports/*` API — GETs are origin-open exactly like the
existing read model (`/state`, `/report`, `/entries`), panel security
unchanged. Every payload is server-computed over the composition's OWN
event log (never a mock/demo source), carries `mode` and the UI-25 trust
block, and renders Decimals as strings with ET-native date/timestamp fields
(the bot's own `date`/`at` values are already ET — see DAY-03).

This module holds no broker reference for any of the GET endpoints: they only
READ the events list handed to it at construction (doc 10 Principle 1) and the
pure `meic.reporting` package. RPT-15's broker fetch lives entirely in
`application/report_reconciler.py`, wired separately by server.py; nothing
in that flow reaches through here.

RPT-16 (proposed amendment, AMENDMENT-PROPOSAL-historical-backfill.md) is the
one deliberate exception: `build_reports_router` optionally accepts a narrow,
duck-typed `broker_reads` facade (only `day_fills` + `day_settlements`,
mirroring `application.backfill.BackfillBrokerFacade`) for the mutating,
auth-gated POST /reports/backfill/{day} endpoint. Every other route is unaffected and
`broker_reads` defaults to None (paper/no-broker composition roots simply
never wire it -- the endpoint then 400s rather than reaching for a broker
that isn't there).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Response

from meic.adapters.api.app import _card_legs, _premium_received
from meic.domain.events import (
    CorrectionRecord,
    EntryMarkSample,
    Event,
    ExternalFillImported,
    LongSaleStarted,
    LongSold,
    ReconciliationMismatch,
    ShortStopped,
    SideUnprotected,
    StopPlaced,
    WatchdogEscalated,
)
from meic.domain.projection import fold
from meic.reporting import slippage as slippage_mod
from meic.reporting.corrections import broker_reconciled_days, corrections_for_day
from meic.reporting.folds import (
    contracts_of,
    core_results,
    entries_by_day,
    entry_credit_dollars,
    entry_day,
    entry_dollars,
    entry_trading_fees_dollars,
    imported_day_fees,
    imported_day_net,
    imported_fills_by_day,
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


def _summary_settlement_pending(events: list[Event], scoped: list[Event]) -> bool:
    """PNL-05 (EOD-01 v1.59 + PNL-04): is the period's HEADLINE P&L provisional?

    True iff some filled entry in scope still has an uncaptured settlement
    (`EntryProjection.settlement_pending`) AND its day has NOT been
    broker-reconciled.

    WHY the settlement half: a held-to-expiry short's loss arrives ONLY with
    its broker settlement row. Until that row is captured, the bot's own fold
    sees the entry credit and nothing else -- the real 2026-07-09 shape is a
    fold saying +$360 on a day whose truth is -$13.88. A credit-only figure
    must never read as final.

    WHY the reconciliation half (the 2026-07-10 live-deploy fix): once RPT-15
    has reconciled the day against the broker -- a `DayBrokerConfirmed`, or an
    own-scoped `CorrectionRecord` -- the DISPLAYED figures ARE broker truth
    (PNL-04), independently established, whatever the bot did or didn't capture
    itself. The real 2026-07-10 day nets +43.68 broker-verified, and its entry's
    `settlement_pending` is true only because the bot never bothered to capture
    the WORTHLESS ($0) expiration rows. Flagging that day provisional would be a
    false alarm -- and a warning that fires on correct days trains the operator
    to ignore it, which is worse than no warning at all.

    `events` (the WHOLE log, not `scoped`) supplies the reconciliation signals,
    same convention as `_trust_payload`.
    """
    reconciled = broker_reconciled_days(events)
    return any(e.settlement_pending and entry_day(e.entry_id) not in reconciled
               for e in _filled_entries(scoped))


def _summary_core(scoped: list[Event]) -> dict[str, Any]:
    r = core_results(scoped)
    return {
        "net_pnl": str(r.net_pnl), "gross_pnl": str(r.gross_pnl), "fees": str(r.fees),
        "filled": r.filled, "fired": r.fired, "skipped_by_reason": r.skipped_by_reason,
        "total_credit": str(r.total_credit),
        "day_win_rate": _s(r.day_win_rate), "entry_win_rate": _s(r.entry_win_rate),
        "premium_capture": _s(r.premium_capture),
        # RPT-16: broker-imported days' contribution -- already folded into
        # net_pnl/gross_pnl/fees above; broken out here too so the dashboard
        # can label it (e.g. "1 imported day") without re-deriving it.
        "imported_days": r.imported_days, "imported_fills": r.imported_fills,
        "imported_net": str(r.imported_net), "imported_fees": str(r.imported_fees),
    }


def _summary_metrics(scoped: list[Event], cfg: ReportingConfig) -> dict[str, Any]:
    from meic.reporting.folds import daily_net

    if cfg.capital_base is None:
        return {"status": "unconfigured"}  # RPT-04/doc-06: required, never a fake denominator
    daily = daily_net(scoped)
    # RPT-16 rule 3: a broker-imported day carries no recorded entry-level
    # intent (targets, probes, stop timing) -- not reconstructable honestly --
    # so it is EXCLUDED from every strategy-quality metric input here
    # (Sharpe/Sortino/MDD/expectancy/streaks all derive from `values` below).
    imported_days = {e.day for e in scoped if isinstance(e, ExternalFillImported)}
    ordered_days = sorted(d for d in daily if d not in imported_days)
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
    # EOD-01 v1.59: ENTRY-side fees only here -- a captured settlement's own
    # fee is already netted INTO `settlements` below (`entry.settlements` is
    # the broker's `value`, net of fee); folding it into `fees` too would
    # double-count it (see entry_trading_fees_dollars's docstring).
    fees = sum((entry_trading_fees_dollars(e) for e in entries), Decimal("0"))
    # `buybacks`/`slippage` are not yet separated from `stop_costs` at the
    # EntryProjection granularity this slice has (that split needs per-fill
    # initiator-tagged pricing beyond CondorFilled/ShortStopped's current
    # fields) -- pinned at $0 here; this reconciles EXACTLY (never a silent
    # residual) because it is the same arithmetic core_results already uses,
    # just not yet decomposed into all of RPT-11's labelled bars. Listed as a
    # follow-up in the slice-2 handoff.
    buybacks = Decimal("0")
    net_slippage = Decimal("0")
    # EOD-01 v1.59: captured broker settlement cash, ALREADY real dollars and
    # net of its own fee -- its own bar so the waterfall still reconciles to
    # the cent on a day with an ITM-expiring settlement.
    settlements = sum((e.settlements for e in entries), Decimal("0"))
    # RPT-16: computed straight from `entries` (like credits/stop_costs/
    # recoveries/fees above), NOT `core_results(scoped).net_pnl` -- that
    # figure now also includes any broker-imported day's cash (RPT-16 rule 3
    # excludes imports from this entry-only waterfall), and mixing the two
    # would produce a spurious WaterfallResidualError whenever an imported
    # day is in scope even though nothing here is actually unreconciled.
    expected_net = sum((entry_dollars(e) for e in entries), Decimal("0"))
    try:
        wf = build_waterfall(credits=credits, stop_costs=stop_costs, recoveries=recoveries,
                             buybacks=buybacks, fees=fees, slippage=net_slippage,
                             settlements=settlements, expected_net=expected_net)
    except WaterfallResidualError as exc:
        return {"error": "residual", "residual": str(exc.residual),
                "expected_net": str(exc.expected_net), "computed_net": str(exc.computed_net)}
    return {
        "credits": str(wf.credits), "stop_costs": str(wf.stop_costs),
        "recoveries": str(wf.recoveries), "buybacks": str(wf.buybacks),
        "fees": str(wf.fees), "slippage": str(wf.slippage), "net": str(wf.net),
        "settlements": str(wf.settlements),
        "premium_capture": _s(wf.premium_capture),
    }


def _trust_payload(events: list[Event], days: tuple[str, ...]) -> dict[str, Any]:
    t = trust_stamp(events, days)
    return {"status": t.status, "confirmed_days": t.confirmed_days,
            "total_days": t.total_days, "label": t.label, "imported_days": t.imported_days}


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


def _long_recovery_rows(scoped: list[Event]) -> list[dict[str, Any]]:
    """RPT-07 long recovery, one row per `LongSold`, journaled events ONLY:

    - `mark_mid`: the long's mid at ladder start, from the LAST `LongSaleStarted`
      journaled for this (entry_id, side) before the sale -- None for a
      pre-stamping event (mark_bid/mark_ask absent on decode). EC-LEX-08
      (v1.64): when NO `LongSaleStarted` was ever journaled for this
      `LongSold` at all -- the floor-fill path (`record_floor_sold` appends
      `LongSold`+`SideClosed` but never stamps `LongSaleStarted`; there was no
      bid/ask to honestly stamp) -- `mark_mid` is the literal sentinel string
      `"no mark (no bid)"` (RPT-07) rather than `None`: an explained gap, not
      a silently missing one.
    - `realized`: `LongSold.recovery` -- always present, it IS the sale.
    - `diff` = realized - mark_mid, None whenever mark_mid is None OR the
      sentinel (never fabricated from a missing mark or from the sentinel).
    - `markup`: the STP-02b buffer in force, from the LAST `StopPlaced`
      journaled for this (entry_id, side) -- None for a pre-stamping event.
    - `shortfall` = markup - realized, None unless both are known.
    - `nle_estimate`: ALWAYS None here -- see the module note on
      `_long_recovery_family` below (no production path journals one).
    """
    NO_MARK_SENTINEL = "no mark (no bid)"  # RPT-07 / EC-LEX-08(v1.64)
    last_start: dict[tuple[str, str], LongSaleStarted] = {}
    last_stop: dict[tuple[str, str], StopPlaced] = {}
    rows: list[dict[str, Any]] = []
    for e in scoped:
        if isinstance(e, LongSaleStarted):
            last_start[(e.entry_id, e.side)] = e
        elif isinstance(e, StopPlaced):
            last_stop[(e.entry_id, e.side)] = e
        elif isinstance(e, LongSold):
            key = (e.entry_id, e.side)
            start = last_start.get(key)
            stop = last_stop.get(key)
            if start is None:
                # EC-LEX-08 (v1.64): no LongSaleStarted at all for this
                # LongSold -- the floor-fill path. An honest, explicit gap,
                # never a fabricated baseline: diff is never computed.
                mark_mid_out: Any = NO_MARK_SENTINEL
                diff = None
            else:
                # Legacy/pre-stamping LongSaleStarted (mark_bid/mark_ask
                # absent on decode): existing behaviour unchanged -- None,
                # NOT the sentinel, which is specifically the no-bid floor
                # case.
                mark_mid = None
                if start.mark_bid is not None and start.mark_ask is not None:
                    mark_mid = (start.mark_bid + start.mark_ask) / 2
                mark_mid_out = _s(mark_mid)
                diff = (e.recovery - mark_mid) if mark_mid is not None else None
            markup = stop.markup if stop is not None else None
            shortfall = (markup - e.recovery) if markup is not None else None
            rows.append({
                "entry_id": e.entry_id, "side": e.side,
                "mark_mid": mark_mid_out, "realized": _s(e.recovery),
                "diff": _s(diff), "markup": _s(markup), "shortfall": _s(shortfall),
                "nle_estimate": None,
            })
    return rows


def _long_recovery_family(scoped: list[Event]) -> dict[str, Any]:
    """RPT-07's long-recovery family + aggregates over the per-row `diff`
    (realized vs mark-at-stop), via the SAME mean/p50/p90/max helpers
    `stop_outs` above uses (one aggregation path, doc 10 RPT-07).

    doc 10 RPT-07 also asks for realized "vs NLE estimate" (NLE-06
    calibration). NO production code path journals an NLE estimate at entry
    time: `domain/nle_calibration.py`'s `CalibrationRecord`/`CalibrationView`
    exist but nothing in `application/` or `composition/` ever constructs
    one outside tests -- `application/nle_preview.py` only computes a live
    UI preview (NLE-05), never journaled. `nle_estimate_captured: False`
    flags this honestly rather than inventing a number; each row's
    `nle_estimate` is always None for the same reason.
    """
    rows = _long_recovery_rows(scoped)
    diffs = [Decimal(r["diff"]) for r in rows if r["diff"] is not None]
    # UI-28 (v1.61): "slippage renders in both ticks and position dollars" --
    # the diff aggregates carry a mean-ticks figure derived EXACTLY the way
    # the stop-outs family derives its own (per-share diff / STOP_TICK, the
    # EC-STP-03 SPX tick this module already pins at 0.05). None when no row
    # carries a diff (pre-stamping rows) -- honest, never fabricated.
    ticks = [d / STOP_TICK for d in diffs]
    return {
        "rows": rows, "n": len(rows),
        "mean": _s(slippage_mod.mean(diffs)), "p50": _s(slippage_mod.p50(diffs)),
        "p90": _s(slippage_mod.p90(diffs)), "max": _s(slippage_mod.maximum(diffs)),
        "mean_ticks": _s(slippage_mod.mean(ticks)),
        "nle_estimate_captured": False,
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
        "long_recovery": _long_recovery_family(scoped),
        # Closes / decay-buyback families need per-fill target-price capture
        # this slice's event schema does not yet record -- deferred (slice-2
        # handoff), never fabricated.
        "closes": None, "decay_buybacks": None,
    }


_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def build_reports_router(
    events: list[Event],
    *,
    mode: Callable[[], str],
    config: ReportingConfig,
    now: Callable[[], str] | None = None,
    broker_reads: Any = None,  # RPT-16: optional BackfillBrokerFacade (day_fills +
    # day_settlements only) --
    # None (the default, and what paper/no-broker roots pass) makes the
    # backfill endpoint 400 rather than reaching for a broker that isn't
    # there. See module docstring for why this is the one deliberate
    # exception to "this module holds no broker reference".
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
            # PNL-05: top-level, not inside `core` -- it qualifies the WHOLE
            # period, not one metric. See _summary_settlement_pending for the
            # rule and why an already-broker-reconciled day is NOT provisional.
            "settlement_pending": _summary_settlement_pending(events, scoped),
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
        imported_fills = imported_fills_by_day(scoped).get(iso_date, ())
        # RPT-16: an imported-only day has no fold entries/skips at all (it
        # was never armed/attempted by this process) -- without this it
        # would 404 as "no data", even though the broker plainly moved money
        # that day.
        if (day_state.date is None and not day_state.entries and not day_state.skipped
                and not imported_fills):
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
                # EOD-01 v1.59: True while a held-to-expiry short's settlement
                # cash has not yet been captured from the broker.
                "settlement_pending": e.settlement_pending,
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
            # RPT-16: broker-imported fills for this day (cash-level only --
            # no recorded entry intent, so these never appear in `entries`
            # above). Empty list on a normal (non-imported) day.
            "imported_fills": [{
                "order_id": f.order_id, "symbol": f.symbol, "action": f.action,
                "quantity": f.quantity, "price": _s(f.price), "fee": _s(f.fee), "at": f.at,
                # RPT-16 (operator ruling 2026-07-10): present only for a
                # broker Receive-Deliver settlement row (cash-settled
                # assignment / expiration) -- the broker's own net cash
                # effect, distinct from a Trade-style fill's price/quantity.
                "value": _s(f.value),
            } for f in imported_fills],
            "imported_cash": None if not imported_fills else {
                "net": str(imported_day_net(imported_fills)),
                "fees": str(imported_day_fees(imported_fills)),
            },
        }

    @router.post("/backfill/{day}")
    async def backfill(day: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """RPT-16: one-time, operator-triggered import of pre-journal broker
        history for `day`, restricted to the operator-supplied `order_ids`
        (OWN-03). Mutating POST -> gated by the SAME auth/origin middleware
        as every other command (adapters/api/app.py's security middleware)."""
        if not _DAY_RE.fullmatch(day):
            raise HTTPException(status_code=400, detail="bad_day_format")
        if broker_reads is None:
            raise HTTPException(status_code=400, detail="backfill_unavailable_no_broker")
        order_ids = set((body or {}).get("order_ids") or [])
        if not order_ids:
            raise HTTPException(status_code=400, detail="order_ids_required")

        from meic.application.backfill import backfill_day

        def _now_iso() -> str:
            return datetime.now().astimezone().isoformat()

        return await backfill_day(events, broker_reads, day, order_ids, now_iso=_now_iso)

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
            from meic.reporting.folds import daily_net, entries_win_loss_by_day

            writer = csv.writer(buf)
            # UI-26a (v1.61): `entries` -- the day's filled-entry count, from
            # the SAME entries_win_loss_by_day fold the win/loss split comes
            # from (RPT-09a: one aggregation path) -- feeds the heatmap hover.
            writer.writerow(["date", "mode", "net_pnl", "trust", "wins", "losses", "entries"])
            daily = daily_net(scoped)
            win_loss = entries_win_loss_by_day(scoped)
            imported = imported_fills_by_day(scoped)
            for d in sorted(daily):
                trust = trust_stamp(events, (d,))
                if d in win_loss:
                    wins, losses, n_entries = win_loss[d]
                elif d in imported:
                    # RPT-16: an imported-only day has no recorded entry-level
                    # outcome to count -- blank (not applicable), never a
                    # fabricated 0/0 for a day that plainly moved money.
                    wins, losses, n_entries = "", "", ""
                else:
                    # A real trading day with zero filled entries (e.g. every
                    # attempt was skipped) truthfully had zero wins/losses --
                    # not a fabrication, same convention as daily_net's $0.00.
                    wins, losses, n_entries = 0, 0, 0
                writer.writerow([d, m, str(daily[d]), trust.status, wins, losses, n_entries])
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
