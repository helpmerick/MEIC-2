import type { PanelState } from "../types";

// Read-only projection of the durable enabling states (ENT-01a/01b, RSK-01) and
// mode (DAY-05). The dashboard names the blocking state when idle (UI-12).

const BLOCKING_LABEL: Record<string, string> = {
  DISARMED: "Disarmed — arm to enable the schedule",
  STOP_TRADING: "Stop Trading is ON — new entries blocked",
  CONFIRM_LIVE_OFF: "Confirm Live is OFF — entries blocked",
};

function Pill({ label, on, kind }: { label: string; on: boolean; kind: "good" | "warn" }) {
  return (
    <span className={`pill ${on ? kind : "off"}`}>
      {label}: <strong>{on ? "ON" : "OFF"}</strong>
    </span>
  );
}

export function Dashboard({ state, connected }: { state: PanelState | null; connected: boolean }) {
  if (!state) {
    return <section className="card"><h2>Status</h2><p className="muted">Connecting…</p></section>;
  }
  const enabled = state.entries_enabled;
  return (
    <section className="card">
      <div className="row between">
        <h2>Status</h2>
        <span className={`pill ${connected ? "good" : "off"}`}>{connected ? "connected" : "offline"}</span>
      </div>

      <div className={`banner ${enabled ? "banner-good" : "banner-idle"}`}>
        {enabled ? "ARMED · firing entries on schedule" : (state.blocking_state ? BLOCKING_LABEL[state.blocking_state] : "idle")}
      </div>

      <div className="pills">
        <span className={`pill ${state.trading_mode === "paper" ? "info" : "warn"}`}>
          mode: <strong>{state.trading_mode.toUpperCase()}</strong>
        </span>
        <Pill label="Armed" on={state.armed} kind="good" />
        <Pill label="Confirm Live" on={state.confirm_live} kind="good" />
        <Pill label="Stop Trading" on={state.stop_trading} kind="warn" />
      </div>
    </section>
  );
}
