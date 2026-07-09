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

export interface EntryCard {
  entry_id: string;
  status: EntryStatus;
  net_credit: string;
  pnl: string;
  sides_stopped: string[];
  sides_expired: string[];
  recovered: boolean;
  close_initiator: string | null;
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
