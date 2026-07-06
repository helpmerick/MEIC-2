import type { PanelState } from "../types";

const BLOCKING: Record<string, { title: string; sub: string }> = {
  DISARMED: { title: "Disarmed", sub: "Arm to enable the standing schedule" },
  STOP_TRADING: { title: "Stop Trading", sub: "New entries are blocked — management continues" },
  CONFIRM_LIVE_OFF: { title: "Confirm Live is OFF", sub: "Entries are blocked until enabled" },
};

function Pill({ label, on, kind }: { label: string; on: boolean; kind: "good" | "warn" }) {
  return (
    <span className={`pill ${on ? kind : ""}`}>
      <span className="pdot" /> {label} · <strong>{on ? "ON" : "OFF"}</strong>
    </span>
  );
}

export function Dashboard({ state, connected }: { state: PanelState | null; connected: boolean }) {
  if (!state) {
    return <section className="card"><h2>Status</h2><p className="muted">Connecting…</p></section>;
  }
  const enabled = state.entries_enabled;
  const block = state.blocking_state ? BLOCKING[state.blocking_state] : null;

  return (
    <section className="card">
      <h2>Status</h2>

      <div className={`hero ${enabled ? "good" : "idle"}`}>
        <span className="hero-icon">{enabled ? "🟢" : "⏸️"}</span>
        <div>
          <div className="hero-title">{enabled ? "Armed · firing on schedule" : block?.title ?? "Idle"}</div>
          <div className="hero-sub">
            {enabled ? "All three enabling states are set" : block?.sub ?? (connected ? "" : "reconnecting…")}
          </div>
        </div>
      </div>

      <div className="pills">
        <span className={`pill ${state.trading_mode === "paper" ? "info" : "warn"}`}>
          <span className="pdot" /> {state.trading_mode.toUpperCase()}
        </span>
        <Pill label="Armed" on={state.armed} kind="good" />
        <Pill label="Confirm Live" on={state.confirm_live} kind="good" />
        <Pill label="Stop Trading" on={state.stop_trading} kind="warn" />
      </div>
    </section>
  );
}
