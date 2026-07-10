# 10 — Results Dashboard (RPT)

**v1.54, operator-ratified 2026-07-10.** Advisor-drafted overnight at the
operator's request; decisions D1–D10 accepted as defaulted; RPT-15 added by the
operator ("no drift between the dashboard and tastytrade/broker truth").

## Principles

1. **Read-only projection.** Everything derives from the event log; the
   reporting module has no broker-gateway dependency for order actions and can
   never place, modify, or cancel an order (structural, as CLS-02's style).
   Its ONLY broker access is the RPT-15 read-only reconciliation fetch.
2. **Broker truth, labeled.** Money numbers come from actual fills (ORD-09,
   v1.52 fill-credit rules) and fees (PNL-01). Days that passed RPT-15/PNL-04
   reconciliation are stamped **broker-confirmed**; others render
   **bot-computed** badges. Trust levels are never silently mixed (UI-25).
3. **Paper and live never commingle.** Every metric/chart/export is
   mode-scoped; PAPER renders under the SIM-05 stamp; no cross-mode sums.
4. **Deterministic replay.** All numbers reproducible by folding the event log
   from genesis (REC-01). Caches disposable; no mutable side-store of truth.
5. **Bot's book only.** FOREIGN positions never enter any metric (OWN-03).

## Rules

- **RPT-01 Period buckets.** ET calendar (DAY-03): Today / day picker / month /
  year / all-time. A reporting **trading day** = any ET day with ≥ 1 entry
  attempt (fired, skipped, or manual) or an open bot position; disarmed flat
  days never dilute averages.

- **RPT-02 Core results per period, per mode.** Net P&L (after PNL-01 fees),
  Gross, Fees, entry counts (fired/skipped-by-reason/filled), contracts,
  win rate by day and by entry, and **premium capture ratio** = Net P&L ÷ total
  credit collected — the headline premium-seller number.

- **RPT-03 Outcome taxonomy & contract conformance.** Every closed entry
  classifies exactly once: FULL_EXPIRY, ONE_SIDE_STOPPED, BOTH_SIDES_STOPPED,
  TPF_CLOSE, TPT_CLOSE (v1.58), DECAY_CLOSE, MANUAL_CLOSE, MANUAL_FLATTEN, EOD_CLOSE,
  INFEASIBLE_STOP, EXTERNAL (operator acted at broker — in cash totals, out of
  strategy-quality metrics, D5). Outcome distribution per period PLUS the
  standing **contract audit** (v1.38, before slippage): ONE_SIDE_STOPPED must
  realize ≥ (1−pct)×credit − recorded slippage; BOTH_SIDES_STOPPED ≥
  −(2·pct−1)×credit − recorded slippage. Breaches render red flags with fill
  drill-down. Expected count: zero, forever.

- **RPT-04 Return & risk.** Denominator `config.reporting_capital_base`
  (operator-set $, D1 — account net-liq rejected: foreign capital would
  pollute ROC), with a secondary view against peak margin actually deployed.
  Metrics (Decimal-exact, 2 dp): ROC; Sharpe annualized = mean(daily net ÷
  base − rf_daily) ÷ sample-stdev(ddof=1) × √252; Sortino (downside dev); max
  drawdown ($, % of base, peak-to-trough of cumulative net); profit factor;
  expectancy per filled entry; avg win/loss day; longest losing streak.
  Sharpe/Sortino gate below `report_min_sample_days` (default 20, D2) →
  "insufficient data"; rf default 0% (D3). Pinned vector (TC-RPT-02): base
  10,000; days +400, +20, −360, +400, +20 ⇒ ROC 4.80%, Sharpe 4.79, MDD $360
  (3.60%), PF 2.33, expectancy +$96, day win rate 80%.

- **RPT-05 Targeting quality.** Per side per entry from STK-11 probe logs +
  ORD-09 fills, decomposed by cause: **selection gap** (matched probe − target,
  chain granularity, bounded +0.15/−1.25), **execution gap** (short fill −
  selected mid), **wing drag** (gross short − net credit per side). Probe-depth
  histogram (#1 = exact target), up-probe rate, down-walk depth,
  no_valid_strikes rate.

- **RPT-06 Slippage in.** Fill credit − first-rung credit (positive = price
  improvement — v1.52's 3.50→3.60 is +0.10 and must display as such);
  rung-count histogram; unfilled_at_floor rate; mean seconds-to-fill.

- **RPT-07 Slippage out**, four families kept separate: stop-outs (EC-STP-03:
  fill − trigger, $ and ticks, mean/p50/p90/max, gap events); long recovery
  (LEX realized vs mark-at-stop and vs NLE estimate — NLE-06 calibration
  surfaced, feeding the stop_rebate_markup decision); closes (fill vs mark at
  initiation, by initiator); decay buybacks (fill vs ≤$0.05 target,
  re-inflation re-arms).

- **RPT-08 Operational health.** Skip-reason histogram; watchdog escalations
  (STP-03b calibration evidence); UNPROTECTED events with seconds naked;
  RSK-03 mismatches; ENT-10 day-task crash alerts; ORD-08 taxonomy counts
  (terminal retries must be 0); per-day reconcile status (RPT-15); and the
  **correction count** (RPT-15) — persistently nonzero corrections = systemic
  drift, investigate.

- **RPT-09 Views.** Equity curve with drawdown shading and incident
  annotations (RPT-14); daily P&L calendar heatmap; outcome stacked bars;
  targeting & slippage panels; day drill-down (entries, legs, fills, stops,
  probe logs, skip reasons, day report, RPT-12 timeline).

- **RPT-10 Determinism, export, API.** Replay-from-genesis reproduces every
  number byte-identically. Every table exports CSV (ET timestamps, mode, trust
  stamp). Read-only `/reports/*`; panel security unchanged.

- **RPT-11 P&L attribution waterfall.** Total credit collected → − stop-out
  costs → + long recoveries → − close/decay buybacks → − fees → − net slippage
  → = Net P&L; bars labeled $ and % of collected. MUST reconcile to the cent;
  a residual renders an error state, never a silently adjusted bar.

- **RPT-12 Intraday timeline + excursions.** Day drill-down timeline: SPX line
  09:30–16:00 ET with markers (entry ▲, stop ✖, close ●, watchdog ⚡,
  UNPROTECTED shaded). Per entry **MAE/MFE** from recorded `EntryMarkSample`
  events (D8: 1-minute cadence per open entry + SPX spot — journals what the
  TPF monitor already computes; if samples are absent the view degrades to
  markers-only, D10: no interpolation ever, gaps render as gaps). Aggregate:
  trigger-distance-consumed distribution, survived vs stopped — the empirical
  read on the stop_loss_pct dial.

- **RPT-13 Slot & regime analytics.** Win rate / expectancy / stop-out rate /
  premium capture per scheduled slot (manual entries group under "manual");
  per day-of-week; per VIX regime (<15, 15–20, 20–25, >25; VIX-at-open from
  the ENT-06 source, D9).

- **RPT-14 Records & annotations.** Best/worst day and month, longest green
  streak, largest entry win/loss, max-drawdown date range; equity-curve
  markers on days with UNPROTECTED / watchdog / contract-breach events.

- **RPT-15 End-of-day broker reconcile-and-correct (operator rule: zero
  drift).** After EOD settlement (EOD-01) each trading day, the dashboard's
  day numbers are verified against the broker: positions (flat/held check),
  the day's fills, cash delta, and fees, via a READ-ONLY fetch — **the cash
  check spans EVERY cash-affecting transaction class for bot symbols: trades,
  fees, AND cash settlements / Receive-Deliver records (v1.59 — settlements
  post outside the trade window; a checker that reads trades only will
  false-confirm an ITM expiry). A day with any bot position expiring ITM MUST
  NOT stamp broker-confirmed until its settlement records are matched.** **Match** ⇒
  the day is stamped broker-confirmed (UI-25). **Mismatch** ⇒ the dashboard
  CORRECTS to broker truth — never silently: a `CorrectionRecord` event enters
  the log (replay-safe) storing both values and the diff; an alert fires; the
  drill-down shows bot-computed vs broker side by side. **Broker unreachable**
  ⇒ the day stays bot-computed (never auto-confirmed) and reconciliation
  retries at next boot/reconcile. Corrections are surfaced in RPT-08; the
  expected steady-state correction count is zero — recurring corrections mean
  a projection bug and block nothing but demand investigation. Extends PNL-04
  (broker authority) from the day report to every dashboard number.

## UI rules (also in doc 03)

- **UI-25 Trust & mode presentation.** Every metric block carries its badge:
  broker-confirmed ✓ (all days in scope passed RPT-15) or bot-computed with
  unreconciled-day count ("22/23 days broker-confirmed"). PAPER banners per
  SIM-05.
- **UI-27 Placement (operator rule, 2026-07-10).** The dashboard is a
  **separate page (client-side route) inside the existing single-page
  application** — not a new app, not widgets bolted onto the trading screen.
  It shares the app shell, panel auth, and backend connections; navigation
  between the Trading page and the Results page is instant client-side
  routing; the trading page keeps only its existing day-report link plus a
  nav entry to Results. Deep links (e.g. a specific day's drill-down) are
  routable URLs.
- **UI-26 Visual standards.** Fixed layout bands: ① Today hero (net so far,
  realized/unrealized, fired/remaining + UI-24 countdown, risk-used vs
  max_day_risk gauge) → ② Performance (equity, heatmap, records) →
  ③ Execution quality (waterfall, targeting, slippage) → ④ Health. Color
  semantics fixed (green realized gain / red realized loss / amber unrealized
  / grey idle), colorblind-safe, never color alone; chart text black-on-light
  (the diagram lesson); every timestamp ET + UI-23 local echo; hover = exact
  Decimal values; numbers animate only on data change.

## Config (also in doc 06)

| Key | Range | Default | Applies | Rule |
|---|---|---|---|---|
| `reporting_capital_base` | $ > 0 | required for return metrics | immediate | RPT-04 |
| `sharpe_risk_free_pct` | 0–10 step 0.25 | 0 | immediate | RPT-04 |
| `report_min_sample_days` | 5–100 | 20 | immediate | RPT-04 |

## Ratified decisions log

D1 capital base = operator-set $ (net-liq rejected: foreign capital).
D2 Sharpe floor 20 trading days. D3 rf default 0%. D4 ET day boundaries.
D5 EXTERNAL in cash, out of quality metrics. D6 no weekly buckets (add later
if wanted). D7 three config keys above. D8 `EntryMarkSample` event (1-min,
bounded, replay-safe; markers-only fallback specced). D9 VIX-at-open from
ENT-06 source. D10 MAE/MFE from recorded samples only — no interpolation.
RPT-15 operator-added 2026-07-10: EOD broker reconcile-and-correct, zero drift.
