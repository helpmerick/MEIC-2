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

// Status content only — no card wrapper. ControlPanel composes this above the
// commands in one shared card (operator request 2026-07-12).
export function Dashboard({ state, connected }: { state: PanelState | null; connected: boolean }) {
  if (!state) {
    return <p className="muted">Connecting…</p>;
  }
  const enabled = state.entries_enabled;
  const block = state.blocking_state ? BLOCKING[state.blocking_state] : null;
  // The status signal: 🎯 locked-on & firing when armed, an amber dot when a
  // gate is holding entries, a grey dot when we've lost the connection
  // (operator chose the 🎯 armed emoji — 2026-07-12).
  const orb = !connected ? "off" : enabled ? "live" : "hold";
  const STATUS_EMOJI: Record<string, string> = { live: "🎯", hold: "🟡", off: "⚪" };
  const STATUS_LABEL: Record<string, string> = {
    live: "armed, firing on schedule",
    hold: "entries held by a gate",
    off: "disconnected",
  };

  return (
    <>
      {/* CAL-08 (v1.71): whenever the current ET day carries a NO-TRADE tag,
          the trading panel says so plainly — the frontend never computes the
          ET day itself (DAY-03), it only renders what /state already read
          off the calendar gate's own fail-open input. */}
      {state.today_blackout_label && (
        <div className="cal-blackout-banner" data-testid="today-blackout-banner" role="status">
          Today: NO-TRADE — {state.today_blackout_label}
        </div>
      )}
      <div className={`hero ${enabled ? "good" : "idle"}`}>
        <span className={`status-emoji ${orb}`} role="img" aria-label={STATUS_LABEL[orb]}>
          {STATUS_EMOJI[orb]}
        </span>
        <div>
          <div className="hero-title">{enabled ? "Armed · firing on schedule" : block?.title ?? "Idle"}</div>
          <div className="hero-sub">
            {enabled ? "All three enabling states are set" : block?.sub ?? (connected ? "" : "reconnecting…")}
          </div>
        </div>
      </div>

      <div className="pills">
        <span className={`pill ${state.trading_mode === "paper" ? "info" : "live-mode"}`}>
          <span className="pdot" /> {state.trading_mode.toUpperCase()}
        </span>
        <Pill label="Armed" on={state.armed} kind="good" />
        <Pill label="Confirm Live" on={state.confirm_live} kind="good" />
        <Pill label="Stop Trading" on={state.stop_trading} kind="warn" />
      </div>
    </>
  );
}
