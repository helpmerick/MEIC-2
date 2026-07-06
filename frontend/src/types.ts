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
