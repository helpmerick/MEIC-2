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
  can_fire: boolean;
}

export interface FireResult {
  result: "filled" | "skipped" | "blocked" | "not_confirmed" | "duplicate_press" | "unavailable";
  reason?: string;
  entry_id?: string;
  initiator?: string;
  fill_credit?: string;
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

// RPT-07's four slippage-out families. Three are `null` in this slice — a
// real API gap (event schema doesn't yet record mark-at-stop/target-price
// capture for these) — rendered as honest "not yet captured" states, never 0.
export interface DaySlippageFamilies {
  stop_outs: StopOutSlippage;
  long_recovery: null;
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
export interface DailyRow {
  date: string;
  mode: string;
  net_pnl: string;
  trust: string;
}
