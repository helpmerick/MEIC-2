import { useState } from "react";
import { api, ApiError, STOP_PCT_SET } from "../api";
import type { PanelState } from "../types";

// Command endpoints (config, stop trading, arm, confirm live). No trading logic:
// each button POSTs; the backend validates and returns the new state. Dangerous
// actions confirm first.

export function CommandPanel({ state, onChange }: { state: PanelState | null; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [pct, setPct] = useState(95);

  async function run(name: string, fn: () => Promise<unknown>, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(name);
    setMsg(null);
    try {
      await fn();
      await onChange();
    } catch (e) {
      setMsg(e instanceof ApiError ? `Rejected: ${JSON.stringify(e.detail)}` : String(e));
    } finally {
      setBusy(null);
    }
  }

  const armed = state?.armed ?? false;
  const stopTrading = state?.stop_trading ?? false;
  const confirmLive = state?.confirm_live ?? false;

  return (
    <section className="card">
      <h2>Commands</h2>

      <div className="cmd-grid">
        <button className="btn" disabled={busy !== null || armed}
          onClick={() => run("arm", api.arm)}>Arm</button>
        <button className="btn" disabled={busy !== null || !armed}
          onClick={() => run("disarm", () => api.disarm())}>Disarm</button>

        <button className={`btn ${stopTrading ? "btn-active" : ""}`} disabled={busy !== null}
          onClick={() => run("stop", () => api.stopTrading(!stopTrading))}>
          {stopTrading ? "Resume Trading" : "Stop Trading"}
        </button>

        <button className={`btn ${confirmLive ? "btn-active" : ""}`} disabled={busy !== null}
          onClick={() => run("confirm", () =>
            api.confirmLive(!confirmLive),
            !confirmLive ? "Turn Confirm Live ON? Entries can fire once armed." : undefined)}>
          {confirmLive ? "Confirm Live: ON" : "Confirm Live: OFF"}
        </button>
      </div>

      <div className="config">
        <label>
          stop_loss_pct
          <select value={pct} onChange={(e) => setPct(Number(e.target.value))}>
            {STOP_PCT_SET.map((p) => <option key={p} value={p}>{p}%</option>)}
          </select>
        </label>
        <button className="btn" disabled={busy !== null}
          onClick={() => run("config", async () => {
            await api.updateConfig({ stop_loss_pct: pct });
            setMsg(`stop_loss_pct set to ${pct}%`);
          })}>Apply</button>
      </div>

      {msg && <p className="msg">{msg}</p>}
    </section>
  );
}
