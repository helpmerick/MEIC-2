// Types mirror the FastAPI read-model contract (backend/src/meic/adapters/api/app.py).
// In production these are generated from the backend's OpenAPI schema (doc 05 §8);
// hand-written here to match /state and /report exactly.

export type BlockingState = "DISARMED" | "STOP_TRADING" | "CONFIRM_LIVE_OFF" | null;

export interface PanelState {
  armed: boolean;
  stop_trading: boolean;
  confirm_live: boolean;
  trading_mode: "paper" | "live";
  entries_enabled: boolean;
  blocking_state: BlockingState;
  // CAL-08 (v1.71, slice-2 additive backend field): today's ET NO-TRADE
  // label, or null when today is untagged/uncalendared. Drives the Trading
  // panel's "Today: NO-TRADE — <label>" banner (Dashboard.tsx) and the CAL-06
  // manual-fire dialogs' warning, without the frontend ever computing an ET
  // trading day itself (UI-03/DAY-03).
  today_blackout_label?: string | null;
}

export interface DayReport {
  date: string | null;
  entries_filled: number;
  stops_hit: number;
  lex_recoveries: number;
  decay_closes: number;
  total_credit: string;
  total_fees: string;
  day_pnl: string;
  skips: [number, string][];
  per_entry_pnl: Record<string, string>;
}

export type EntryStatus =
  | "PENDING" | "PROTECTED" | "STOPPED" | "LEX_RECOVERED"
  | "EXPIRED" | "DECAY_CLOSED" | "CLOSED";

// FEATURE 2 (v1.46 card): one broker-allocated leg — strike parsed from the OCC
// symbol, price null when the broker reported no allocation (paper/simulated).
export interface EntryLeg {
  side: "PUT" | "CALL";
  role: "short" | "long";
  strike: string;
  price: string | null;
  qty: number;
}

export interface EntryCard {
  entry_id: string;
  status: EntryStatus;
  net_credit: string;
  pnl: string;
  sides_stopped: string[];
  sides_expired: string[];
  recovered: boolean;
  close_initiator: string | null;
  // FEATURE 1: fill time (ISO, with offset), null until a fill is recorded.
  placed_at?: string | null;
  // FEATURE 2: per-side strikes/prices, and the derived premium per side (null
  // when either leg's price is unknown — paper carries no allocation).
  legs?: EntryLeg[];
  premium_received?: { PUT: string | null; CALL: string | null };
  // FEATURE 3 (live only; paper always sends these absent/null — no fabricated
  // estimate). Updates on the ~60s health-loop cadence; null means "—", never a
  // guess (stale/absent snapshot or a mark outside the ATM band).
  live_pnl?: string | null;
  live_pnl_asof?: string | null;
  // EOD-01 v1.59: True while a held-to-expiry short's broker settlement
  // cash has not yet been captured -- this entry's P&L is provisional.
  settlement_pending?: boolean;
  // v1.58 TPF/TPT: current whole-entry profit% (TPF-01's shared evaluator),
  // live only -- null means unavailable (paper, stale/no snapshot), never a
  // guess (same convention as live_pnl above).
  profit_pct?: string | null;
  // TPF-06: the armed floor level (percent), or null/absent if not armed.
  tpf_floor?: number | null;
  // TPT-02: the armed target level (percent), or null/absent if not armed.
  tpt_target?: number | null;
  // TPT-05: permanently disarmed the moment ANY stop fills on this entry.
  tpt_disarmed?: boolean;
  // TPT-06: "closes at debit <= $D (keep >= $P)" dollar feedback, present
  // only while a target is armed.
  tpt_feedback?: { debit: string; keep: string };
}

export interface ActivityLine {
  icon: string;
  label: string;
  entry: string;
  detail: string;
}

export interface Snapshot {
  state: PanelState;
  report: DayReport;
  entries: EntryCard[];
  activity: ActivityLine[];
}

// --- UC-02 schedule panel (v1.44/v1.46) --------------------------------------
// The row shape the backend validates. An empty cell means "inherit the global"
// (doc 06 section 37) — it is never zero. Only `time` is required.

export interface ScheduleRow {
  time: string;                 // "HH:MM" ET
  contracts?: number | "";      // ENT-04: 1-10, pre-filled from contracts_per_entry
  target_premium?: string | ""; // STK-02: 0.50-20.00
  wing_width?: string | "";     // STK-03: 10-200 step 5
  stop_loss_pct?: number | "";  // STP-02: the discrete set
  stop_basis?: string;          // total_credit | short_premium (per_side is rejected)
  stop_rebate_markup?: string | "";
  worst_case_estimate?: string; // server-computed, read-only
  effective_stop_pct_estimate?: string | null; // STP-02b (v1.67), server-computed, read-only
  id?: number;                   // ENT-10(4)/v1.53: durable entry id, assigned at Save.
                                 // Round-trips unedited through patch()'s row spread so a
                                 // re-save never renumbers a row the operator didn't touch.
}

export interface ScheduleView {
  rows: ScheduleRow[];
  day_total_estimate: string;
  max_day_risk: string | null;
  headroom: string | null;
  exceeds_max_day_risk: boolean;
  config_version: string | null;
  estimate_note: string;
  risk_scope_note: string;
}

export interface ScheduleError {
  field: string;
  reason: string;
  index: number | null;
}

// UC-02 arm pre-flight (backend/src/meic/application/preflight.py)
export interface PreflightCheck {
  name: string;
  rule: string;
  passed: boolean;
  detail: string;
}

export interface Preflight {
  passed: boolean;
  checks: PreflightCheck[];
  blocked_by: string | null;
}

// UI-22: what the ENT-09 confirmation dialog shows. The worst case is an
// ESTIMATE — no strikes exist at press time (v1.46). RSK-04 re-prices from real
// strikes at fire time and may still veto.
export interface FirePreview {
  press_id: string;
  entry_number: number;
  now: string;
  contracts: number;
  target_premium: string;
  wing_width: string;
  stop_loss_pct: number;
  worst_case_estimate: string;
  worst_case_is_estimate: true;
  estimate_formula: string;
  effective_stop_pct_estimate: string | null; // STP-02b (v1.67)
  can_fire: boolean;
}

export interface FireResult {
  result: "filled" | "skipped" | "blocked" | "not_confirmed" | "duplicate_press" | "unavailable";
  reason?: string;
  entry_id?: string;
  initiator?: string;
  fill_credit?: string;
  // CAL-06 (v1.71): present on a `skipped` result refused for lack of the
  // blackout acknowledgment checkbox (reason "blackout_unacknowledged:<label>").
  blackout_label?: string;
  // CAL-06: true on a `filled` result that proceeded via an ACKNOWLEDGED
  // override of today's NO-TRADE tag — never present/true otherwise.
  blackout_overridden?: boolean;
}

// ENT-09b (v1.57): the ▶ dialog's floor dropdown candidates, from the entry's
// live VALIDATED UNIVERSE — puts descending from the money, calls ascending,
// each with its distance from spot (points) and live mid (backend/composition/
// live_selection.py: floor_candidates). `available: false` (no provider wired,
// e.g. paper) or an empty side list (no snapshot yet / stale) both mean the
// dropdown has nothing honest to offer.
export interface FloorCandidateRow {
  strike: string;
  distance_pts: string;
  mid: string;
}

export interface FloorCandidates {
  available: boolean;
  put?: FloorCandidateRow[];
  call?: FloorCandidateRow[];
  spot?: string | null;
  quote_at?: string | null;
}

// ENT-11/UI-25: the ad-hoc manual-trade card's Simulate result. Read-only — it
// places no order and appends no event. `worst_case` here is REAL (computed from
// the selector's actual strikes), unlike the schedule row's pre-selection
// formula estimate; still labelled an estimate because the real fire re-selects
// from fresh data and may differ.
export interface ManualSimulation {
  result: "ok" | "skipped";
  reason?: string;
  put_short?: string;
  put_long?: string;
  call_short?: string;
  call_long?: string;
  put_mid?: string;
  call_mid?: string;
  net_credit?: string;
  worst_case?: string;
  contracts?: number;
  estimate_note?: string;
}

// ENT-10 / UI-24: the day supervisor's watch state (backend/adapters/api/server.py
// GET /day/status). `next_entry_at` is an ET ISO datetime; the countdown itself
// is display-only (UI-03) — the backend's `seconds_to_next` is authoritative.
export interface DayStatus {
  started: boolean;
  running: boolean;
  armed?: boolean;
  next_entry_at?: string | null;
  seconds_to_next?: number | null;
  entries_remaining?: number;
  cancelled?: boolean;
  error?: string | null;
  filled?: number | null;
  // RSK-06: the supervisor's last tick failure (repr), null when healthy.
  supervisor_error?: string | null;
}

// =============================================================================
// Slice 3 — Results dashboard (doc 10 RPT-*, UI-25/26/27). Types mirror
// backend/src/meic/adapters/api/reports.py's dict shapes exactly — every
// money/ratio field is a Decimal-as-string (UI-03: no client float re-derivation).
// =============================================================================

// RPT-15/UI-25 — every metric block's trust chip. RPT-16 (proposed amendment)
// adds "broker-imported": a day imported from broker history is broker truth
// by construction, but never "broker-confirmed" (that label means the bot's
// OWN computation matched the broker's, which never happened for an import).
export interface TrustBlock {
  status: "broker-confirmed" | "bot-computed" | "broker-imported";
  confirmed_days: number;
  total_days: number;
  label: string;
  imported_days?: number;
}

// RPT-02 core results for the scoped period.
export interface CoreResults {
  net_pnl: string;
  gross_pnl: string;
  fees: string;
  filled: number;
  fired: number;
  skipped_by_reason: Record<string, number>;
  total_credit: string;
  day_win_rate: string | null;
  entry_win_rate: string | null;
  premium_capture: string | null;
}

// RPT-04 return/risk metrics. "unconfigured" when reporting_capital_base is
// unset (doc 06) — the frontend must never fabricate a denominator.
export type MetricsResult =
  | { status: "unconfigured" }
  | {
      status: "ok";
      roc: string | null;
      sharpe: string | null;
      sortino: string | null;
      max_drawdown_dollars: string;
      max_drawdown_pct: string;
      profit_factor: string | null;
      expectancy_per_entry: string | null;
      avg_win_day: string | null;
      avg_loss_day: string | null;
      longest_losing_streak_days: number;
      day_win_rate: string | null;
      sample_days: number;
      min_sample_days: number;
    };

// RPT-03 contract-audit breach (expected count: zero, forever).
export interface ContractBreach {
  entry_id: string;
  outcome: string;
  realized: string;
  floor: string;
}

export interface TaxonomyResult {
  distribution: Record<string, number>;
  contract_breaches: ContractBreach[];
}

// RPT-08 operational health. The two `null` fields are known slice-2 API
// gaps (not derivable from the replay log yet) — render as "not yet captured",
// never as zero.
export interface HealthResult {
  skip_reason_histogram: Record<string, number>;
  watchdog_escalations: number;
  unprotected_events: number;
  rsk03_mismatches: number;
  correction_count: number;
  ent10_crash_alerts: number | null;
  ord08_terminal_retries: number | null;
}

// RPT-11 waterfall — a residual is an explicit error state, never a silently
// adjusted bar.
export type WaterfallResult =
  | { error: "residual"; residual: string; expected_net: string; computed_net: string }
  | {
      credits: string;
      stop_costs: string;
      recoveries: string;
      buybacks: string;
      fees: string;
      slippage: string;
      net: string;
      premium_capture: string | null;
      // EOD-01 v1.59: captured broker settlement cash, real dollars, already
      // net of its own fee -- its own waterfall bar.
      settlements?: string;
    };

export interface ReportSummary {
  mode: "paper" | "live";
  period_days: string[];
  trust: TrustBlock;
  // PNL-05 (EOD-01 v1.59): true when ANY entry in the period's scope has an
  // uncaptured broker settlement -- the headline net/gross/fees below are
  // credit-only until then and must render as provisional, not final.
  settlement_pending?: boolean;
  core: CoreResults;
  metrics: MetricsResult;
  taxonomy: TaxonomyResult;
  health: HealthResult;
  waterfall: WaterfallResult;
}

// One day-drill-down entry (GET /reports/day/{iso_date}).
export interface DayEntryDetail {
  entry_id: string;
  status: string;
  net_credit: string;
  pnl: string;
  fees: string;
  sides_stopped: string[];
  sides_expired: string[];
  close_initiator: string | null;
  outcome: string | null;
  legs: EntryLeg[];
  premium_received: { PUT: string | null; CALL: string | null };
  // EOD-01 v1.59: True while a held-to-expiry short's broker settlement
  // cash has not yet been captured -- this entry's P&L is provisional.
  settlement_pending?: boolean;
}

// RPT-12 timeline: one EntryMarkSample (1-min cadence, D8) — every field is
// null-safe; D10 forbids interpolation, so gaps in `marks` render as gaps.
export interface MarkSample {
  entry_id: string;
  at: string;
  spot: string | null;
  put_short_mid: string | null;
  put_long_mid: string | null;
  call_short_mid: string | null;
  call_long_mid: string | null;
}

export interface TimelineMarker {
  type: string;
  icon: string;
  entry_id: string | null;
  at: string | null;
}

export interface StopOutSlippage {
  mean: string | null;
  p50: string | null;
  p90: string | null;
  max: string | null;
  mean_ticks: string | null;
  n: number;
}

// RPT-07 long recovery: one row per LongSold, journaled events only.
// `mark_mid`/`markup` are `null` for a pre-stamping event (event existed
// before the 2026-07-11 mark-at-stop/markup stamping shipped) — never
// fabricated. `diff`/`shortfall` are `null` whenever their inputs are.
// `nle_estimate` is always `null` in this slice: no production code path
// journals an NLE estimate at entry time (see reports.py's module note).
export interface LongRecoveryRow {
  entry_id: string;
  side: string;
  mark_mid: string | null;
  realized: string;
  diff: string | null;
  markup: string | null;
  shortfall: string | null;
  nle_estimate: null;
}

export interface LongRecoveryFamily {
  rows: LongRecoveryRow[];
  n: number;
  mean: string | null;
  p50: string | null;
  p90: string | null;
  max: string | null;
  // UI-28 (v1.61): slippage renders in BOTH ticks and position dollars —
  // derived server-side from the same EC-STP-03 tick (0.05) the stop-outs
  // family uses; null when no row carries a diff (pre-stamping rows).
  mean_ticks: string | null;
  nle_estimate_captured: false;
}

// RPT-07's four slippage-out families. `long_recovery` is populated from
// journaled events (2026-07-11). `closes`/`decay_buybacks` stay `null` — a
// real API gap (event schema doesn't yet record fill-vs-mark-at-initiation /
// fill-vs-target capture for these) — rendered as honest "not yet captured"
// states, never 0.
export interface DaySlippageFamilies {
  stop_outs: StopOutSlippage;
  long_recovery: LongRecoveryFamily;
  closes: null;
  decay_buybacks: null;
}

export interface CorrectionEntry {
  field: string;
  bot_value: string;
  broker_value: string;
  diff: string;
  at: string;
}

// RPT-16 (proposed amendment) — one broker-imported fill leg, rendered on the
// day drill-down for a day that predates the event journal.
export interface ImportedFill {
  order_id: string;
  symbol: string;
  action: string;
  quantity: number;
  price: string | null;
  fee: string | null;
  at: string;
  // RPT-16 settlement import (operator ruling 2026-07-10): present only on a
  // broker Receive-Deliver settlement row (cash-settled assignment /
  // expiration) — the broker's own signed NET cash effect in real dollars.
  value?: string | null;
}

export interface DayReportDetail {
  date: string;
  mode: "paper" | "live";
  trust: TrustBlock;
  entries: DayEntryDetail[];
  skips: { entry_number: number; reason: string }[];
  timeline: { marks: MarkSample[]; markers: TimelineMarker[] };
  slippage: DaySlippageFamilies;
  corrections: CorrectionEntry[];
  imported_fills?: ImportedFill[];
  imported_cash?: { net: string; fees: string } | null;
}

// RPT-10 CSV export's "daily" table — the only per-day net-P&L SERIES the
// backend exposes in this slice (see api.ts's getDailySeries for why the
// equity curve/heatmap parse this instead of a dedicated JSON array).
// `wins`/`losses` (RPT-09 calendar-heatmap hover) mirror `entry_win_rate`'s
// own pnl>0/pnl<0 threshold, per day; both null on a broker-imported day
// (RPT-16: no recorded entry-level outcome exists to count — never a
// fabricated 0/0 for a day that plainly moved real broker cash).
// `entries` (UI-26a v1.61) is the day's filled-entry count, from the SAME
// single aggregation path the win/loss split comes from (RPT-09a) — null on
// a broker-imported day for the same honesty reason.
export interface DailyRow {
  date: string;
  mode: string;
  net_pnl: string;
  trust: string;
  wins: number | null;
  losses: number | null;
  entries: number | null;
}

// =============================================================================
// Slice 2 — Trading calendar (doc 11 CAL-*, UI-30). Types mirror the GET
// /calendar read model exactly (backend/src/meic/adapters/api/app.py
// get_calendar); every day/date key is an ET calendar date string ("YYYY-MM-DD",
// DAY-03) — never a browser-local date.
// =============================================================================

/** One currently-EFFECTIVE tagged day (CAL-03/04). `category` is populated
 * only for an auto-tag (origin "auto"); a manual tag (origin "manual") may
 * still SIT OVER an effective auto-tag underneath it — CAL-04's own layered-
 * removal semantics (slice 1) mean the UI never needs to detect that ahead of
 * time: removing once and re-reading this same field is how the operator SEES
 * the auto-tag persist (or not). */
export interface CalendarTag {
  label: string;
  origin: "manual" | "auto";
  category: string | null;
}

/** CAL-02 per-category staleness + (slice-2 additive) the category's own
 * imported dates, so the year view can mark an event on an imported-but-
 * untagged day (a category with no standing rule is never auto-tagged).
 * `dates` is OPTIONAL in the type because it is additive: a backend that
 * predates it omits the field, and the tab must degrade to "no markers for
 * this category", never crash (final review, 2026-07-15). */
export interface CalendarStaleness {
  imported_at: string;
  horizon: string | null;
  stale: boolean;
  tier: 1 | 2;
  dates?: string[];
}

export interface CalendarData {
  available: boolean;
  tags?: Record<string, CalendarTag>;
  staleness?: Record<string, CalendarStaleness>;
  standing_rules?: Record<string, string | null>;
}

/** DOC-01..05 (doc 12) -- the How-it-works tab's single source (GET /guide).
 * `guide_markdown` is the ratified guide content read straight from
 * spec/12-how-it-works.md (never a frontend copy); `guide_version` is its
 * own "describes spec vX.YY" stamp, `running_spec_version` the RUNNING
 * build's own spec version (spec/README.md's changelog head); `version_mismatch`
 * is the backend's own comparison (DOC-05) -- the frontend renders the
 * banner off this flag rather than re-deriving the comparison itself.
 * `version_unknown` is DOC-05's failure polarity: either version failing to
 * PARSE means the comparison is unverifiable, and the banner must fail
 * toward showing ("cannot verify") -- never toward false currency. */
export interface GuideData {
  guide_markdown: string;
  guide_version: string | null;
  running_spec_version: string | null;
  version_mismatch: boolean;
  version_unknown: boolean;
}
