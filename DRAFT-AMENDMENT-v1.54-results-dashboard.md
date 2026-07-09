# DRAFT spec amendment v1.54 — Results Dashboard (RPT)

**Status: DRAFT — NOT RATIFIED. Advisor-authored 2026-07-10 overnight at the
operator's request. Nothing here is buildable until Ash ratifies; on ratification
the rules move into `spec/` (docs 01/03/04/06), the lock regenerates, and the
changelog gains v1.54.**

Operator brief (verbatim intent): daily / monthly / yearly / all-time results,
Sharpe ratio, return on capital, difference between targeted premium and strikes
selected, slippage on the way in and out, plus anything else useful.

---

## 0. Design principles (inherited from the ratified spec)

1. **The dashboard is a read-only projection.** It derives everything from the
   event log and can never place, modify, or cancel an order — structurally, the
   reporting module has no broker gateway dependency (same enforcement style as
   "UI never has close logic", CLS-02).
2. **Broker truth, labeled.** Every money number is computed from actual fills
   (ORD-09 recorded legs/allocations, v1.52 fill-credit rules) and fees (PNL-01).
   Days that have passed PNL-04 broker reconciliation are stamped
   **broker-confirmed**; days that haven't display **bot-computed** badges.
   The dashboard never silently mixes the two trust levels.
3. **Paper and live never commingle.** Every metric, chart, and export is scoped
   to one mode; PAPER data renders under the existing PAPER stamp (SIM-05) and
   no aggregate ever sums across modes.
4. **Deterministic replay.** All dashboard numbers are reproducible by folding
   the event log from genesis (REC-01). No mutable side-store of truth; caches
   are disposable.
5. **Bot's book only.** FOREIGN positions never enter any metric (OWN-03).

---

## 1. Rules

### RPT-01 — Period buckets
Results are bucketed by **ET calendar** (DAY-03): day, month, calendar year, and
all-time. A **trading day** for reporting purposes is any ET day with ≥ 1 entry
attempt (fired, skipped, or manual) or an open bot position. Days where the bot
was disarmed and flat do not dilute averages. The period selector is exactly:
Today / Day picker / Month / Year / All-time.

### RPT-02 — Core results per period
For each period, computed from fills and fees, per mode:

| Metric | Definition |
|---|---|
| Net P&L | Σ realized cash flows of bot entries − Σ fees (PNL-01), settled per EOD-01 |
| Gross P&L | Net P&L before fees |
| Fees | Σ PNL-01 fee model, actuals once broker-confirmed |
| Entries | count fired (scheduled + manual), count skipped (by reason), count filled |
| Contracts | Σ per-entry contracts filled |
| Win rate (days) | % trading days with Net P&L > 0 |
| Win rate (entries) | % filled entries with realized net > 0 |

### RPT-03 — Outcome taxonomy & contract conformance
Every closed entry is classified into exactly one outcome:
`FULL_EXPIRY`, `ONE_SIDE_STOPPED` (other side expired/closed green),
`BOTH_SIDES_STOPPED`, `TPF_CLOSE`, `DECAY_CLOSE`, `MANUAL_CLOSE`,
`MANUAL_FLATTEN`, `EOD_CLOSE`, `INFEASIBLE_STOP`, `EXTERNAL` (operator acted at
broker — reported but excluded from strategy-quality metrics, included in cash).

The dashboard shows the outcome distribution per period AND a **contract
conformance check** per entry (the v1.38 ratified outcome contract, before
slippage): a `ONE_SIDE_STOPPED` entry must realize ≥ (1 − pct) × net credit
minus recorded slippage; a `BOTH_SIDES_STOPPED` entry must realize ≥
−(2·pct − 1) × net credit minus recorded slippage. Violations beyond recorded
slippage render as red **contract-breach flags** with a drill-down — this is the
standing audit that the stop engine keeps its promise. (Expected count: zero.)

### RPT-04 — Return & risk metrics
Denominator: `config.reporting_capital_base` (operator-set dollars — see §3;
decision D2 below). All return metrics also show a secondary computation
against **peak margin actually deployed** in the period (from SIM-04-style
margin accounting / broker BP snapshots) so "return on capital I allocated"
and "return on capital the strategy actually used" are both visible.

| Metric | Definition (all Decimal-exact, displayed to 2 dp) |
|---|---|
| ROC | period Net P&L ÷ capital base |
| Sharpe (annualized) | mean(daily net ÷ base − rf_daily) ÷ sample-stdev(same, ddof=1) × √252 |
| Sortino (annualized) | same numerator ÷ downside deviation (negative days only) × √252 |
| Max drawdown | peak-to-trough of the cumulative daily-net equity curve, $ and % of base |
| Profit factor | Σ gross positive days ÷ |Σ gross negative days| |
| Expectancy / entry | period Net P&L ÷ filled entries |
| Avg win / avg loss (days) | means of positive / negative day nets |
| Longest losing streak | consecutive negative trading days, count and $ |

**Minimum-sample gate:** Sharpe/Sortino render "insufficient data (n < 20
trading days)" rather than a number below 20 samples; ROC, drawdown, profit
factor render from day 1. rf (risk-free) default 0 (decision D3).

**Worked vector (pinned in TC-RPT-02):** capital base $10,000; five trading
days netting +400, +20, −360, +400, +20 (the canonical v1.38 day types).
Daily returns: 4.00%, 0.20%, −3.60%, 4.00%, 0.20%. Mean 0.96%; sample stdev
3.1793%; daily Sharpe 0.3020; annualized **4.79**. Equity 400→420→60→460→480:
max drawdown **$360 = 3.60%**. Profit factor 840/360 = **2.33**. Expectancy
(entries=5) **+$96**. Win rate (days) **80%**.

### RPT-05 — Targeting quality ("did I get the premium I asked for?")
Per side per entry, from the STK-11 probe log and ORD-09 fills, three separate
numbers — the decomposition matters because they have different causes:

1. **Selection gap** = matched probe price − target T (chain granularity;
   bounded by the ratified walk: +0.15 / −1.25). Display per-side distribution,
   mean, and the **probe-depth histogram** (which probe number matched: #1 = T
   exactly, etc.), plus up-probe rate, down-walk depth, `no_valid_strikes` rate.
2. **Execution gap** = actual short fill premium − selected strike's mid at
   selection. (Entry ladder effect on the short leg.)
3. **Wing drag** = (gross short premium collected − net credit) per side vs the
   fixed $0.50-style assumption — what the longs actually cost.

Example row: T $3.00, matched probe $2.95 (probe #2), short filled $2.93,
long cost $0.55 → selection gap −0.05, execution gap −0.02, wing drag 0.55.

### RPT-06 — Slippage in
Per entry: **fill credit − first-rung (mid) credit** (positive = price
improvement, negative = ladder cost), rung-count histogram (filled at rung 1…5),
`unfilled_at_floor` rate, and mean seconds-to-fill. (v1.52's 3.50→3.60 example
is +0.10 slippage-in — improvement is real and must show as such.)

### RPT-07 — Slippage out
Four exit families, kept separate:

1. **Stop-outs** (EC-STP-03 records): stop fill − trigger, in $ and ticks —
   mean, p50/p90/max, gap-event count (> slippage_alert_ticks).
2. **Long recovery** (LEX): realized long sale vs (a) long mark at stop moment
   and (b) the NLE estimate — this IS the NLE-06 calibration view, surfaced;
   it feeds the operator's `stop_rebate_markup` decision ("data suggests ~$X").
3. **Closes** (replace-based CLS-01, v1.50): close fill vs mark at initiation,
   by initiator (manual / take_profit / eod / decay / infeasible_stop).
4. **Decay buybacks**: fill vs the ≤ $0.05 target; re-inflation re-arms count.

### RPT-08 — Operational health
Per period: skip-reason histogram (every ENT-03/STK/ORD skip reason), watchdog
escalations (STP-03b — each is trigger-source calibration evidence), UNPROTECTED
events with **duration naked** (seconds), reconciliation mismatches (RSK-03),
day-task crash alerts (ENT-10), API cancel-taxonomy counts (ORD-08 terminal
retries must be 0), and broker-reconcile status per day (PNL-04 pass/fail/pending).

### RPT-09 — Views
- **Equity curve** (cumulative net, period-scoped) with drawdown shading.
- **Daily P&L calendar heatmap** (month view: green/red cells, $ on hover).
- **Outcome distribution** stacked bar per period.
- **Targeting & slippage panels** (RPT-05/06/07 tables + histograms).
- **Day drill-down**: click any day → that day's entries, legs, fills, stops,
  probe logs, skip reasons, and its existing day report (PNL-04).

### RPT-10 — Determinism, export, API
Replaying the event log from genesis reproduces every dashboard number
byte-identically (TC-RPT-05). Every table exports CSV (ET timestamps, mode
column, trust stamp column). Served via read-only `/reports/*` endpoints;
panel security rules apply unchanged (NFR/panel token).

### UI-25 — Trust & mode presentation
Every metric block carries its trust badge (**broker-confirmed** ✓ when all
days in scope passed PNL-04; **bot-computed** otherwise, with count of
unreconciled days). PAPER mode banners per SIM-05. A period mixing reconciled
and unreconciled days shows the badge split ("22/23 days broker-confirmed").

---

## 2. Decisions I made overnight — reverse any of these in the morning

- **D1 Capital base**: `reporting_capital_base` is an operator-set dollar
  amount (not auto net-liq) — your account holds non-bot positions, so net-liq
  would pollute ROC with foreign capital. Secondary margin-deployed view keeps
  honesty about actual usage.
- **D2 Sharpe sample floor 20 trading days** — below that the number is noise
  and renders as "insufficient data" instead.
- **D3 Risk-free rate default 0%** (configurable `sharpe_risk_free_pct`,
  0–10, step 0.25) — at 0DTE holding periods the rf adjustment is cosmetic.
- **D4 Day boundaries are ET calendar** (matches DAY-03), not London.
- **D5 EXTERNAL closes count in cash but not in strategy-quality metrics**
  (win rates, conformance) — your manual broker interventions shouldn't grade
  the bot.
- **D6 Weekly buckets omitted** (you asked D/M/Y/All); trivial to add later.
- **D7 New config keys**: `reporting_capital_base` ($, required for ROC/Sharpe,
  no default — like max_day_risk), `sharpe_risk_free_pct` (default 0),
  `report_min_sample_days` (default 20, range 5–100).

## 3. Config rows (→ doc 06 on ratification)

| Key | Range | Default | Applies | Rule |
|---|---|---|---|---|
| `reporting_capital_base` | $ > 0 | required for return metrics | immediate | RPT-04 |
| `sharpe_risk_free_pct` | 0–10 step 0.25 | 0 | immediate | RPT-04 |
| `report_min_sample_days` | 5–100 | 20 | immediate | RPT-04 |

## 4. Test cases (→ doc 04 on ratification)

**TC-RPT-01** — RPT-01/02/UI-25
```gherkin
Scenario: Period buckets and trust stamps
  Given fills across two ET days, one broker-reconciled and one pending
  Then Today shows only today's entries and a bot-computed badge
  And the month view badge reads "1/2 days broker-confirmed"
  And paper fills never appear in live periods or exports

Scenario: Disarmed flat days do not dilute averages
  Given 5 trading days and 2 disarmed flat days in a month
  Then day-based means and win rates use n=5
```

**TC-RPT-02** — RPT-04 pinned return vectors
```gherkin
Scenario: The canonical five-day vector computes exactly
  Given capital base 10000 and daily nets +400, +20, -360, +400, +20
  Then ROC = 4.80%, Sharpe(annualized) = 4.79, max drawdown = $360 (3.60%)
  And profit factor = 2.33, expectancy = +$96/entry, day win rate = 80%

Scenario: Sharpe gates on sample size
  Given 19 trading days
  Then Sharpe and Sortino render "insufficient data" and ROC still renders
```

**TC-RPT-03** — RPT-03 outcome & conformance
```gherkin
Scenario: Outcomes classify exactly once
  Given the $4.00-credit canonical trade stopped on the put side only
  Then the entry is ONE_SIDE_STOPPED with realized >= +$20 minus recorded slippage
  And a both-sides day classifies BOTH_SIDES_STOPPED with realized >= -$360 minus recorded slippage

Scenario: A contract breach flags red
  Given a ONE_SIDE_STOPPED entry whose realized loss exceeds recorded slippage allowance
  Then the dashboard renders a contract-breach flag with a drill-down to its fills
```

**TC-RPT-04** — RPT-05/06/07 decomposition
```gherkin
Scenario: Targeting decomposition separates causes
  Given target 3.00, matched probe 2.95 at probe #2, short filled 2.93
  Then selection gap = -0.05, execution gap = -0.02, probe depth = 2

Scenario: Slippage-in can be positive
  Given first-rung credit 3.50 and fill credit 3.60
  Then slippage-in = +0.10 (price improvement, from the v1.52 incident order)

Scenario: Stop slippage reports from EC-STP-03 records
  Given a stop with trigger 3.80 filled at 3.90
  Then slippage-out = 0.10 = 2 ticks and it enters mean and p90
```

**TC-RPT-05** — RPT-10 determinism
```gherkin
Scenario: Replay reproduces the dashboard
  Given any event log
  When the log is replayed from genesis into a fresh projection
  Then every dashboard number is byte-identical to the incremental projection
```

**TC-RPT-06** — RPT-01 read-only isolation
```gherkin
Scenario: The reporting module cannot trade
  Then the reporting module has no dependency on the broker gateway port
  And no /reports endpoint can mutate state (verified structurally)
```

## 5. Build notes for the agent (post-ratification)

Projection folds live beside the existing P&L fold (domain/projection.py
lineage); probe logs (STK-11), slippage records (EC-STP-03), NLE calibration
(NLE-06), and watchdog escalation events already exist in the log — RPT is a
consumer, not a new writer. Frontend: new Dashboard route; period selector;
recharts-class charts are fine; all numbers server-computed (client renders
only). CSV via the existing panel-auth endpoints.
