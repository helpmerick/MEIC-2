import { useState } from "react";
import { api, ApiError, STOP_PCT_SET } from "../api";
import type { PanelState } from "../types";

// Commands POST to the backend; the UI updates OPTIMISTICALLY for instant
// feedback, then reconciles with the authoritative response. No trading logic
// (UI-03) — the backend validates and is the source of truth.
export function CommandPanel({
  state, optimistic, refresh,
}: {
  state: PanelState | null;
  optimistic: (patch: Partial<PanelState>) => void;
  refresh: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ text: string; err: boolean } | null>(null);
  const [pct, setPct] = useState(95);

  async function run(
    name: string,
    fn: () => Promise<unknown>,
    opts?: { optimistic?: Partial<PanelState>; confirm?: string },
  ) {
    if (opts?.confirm && !window.confirm(opts.confirm)) return;
    setBusy(name);
    setMsg(null);
    if (opts?.optimistic) optimistic(opts.optimistic); // instant
    try {
      await fn();
      refresh();
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? `Rejected: ${JSON.stringify(e.detail)}` : String(e), err: true });
      refresh(); // pull back authoritative state (revert the optimism)
    } finally {
      setBusy(null);
    }
  }

  const armed = state?.armed ?? false;
  const stop = state?.stop_trading ?? false;
  const cl = state?.confirm_live ?? false;
  const label = (n: string, t: string) => (busy === n ? <><span className="spin" />{t}</> : t);

  return (
    <section className="card">
      <h2>Commands</h2>

      <div className="cmd-grid">
        <button className="btn primary" disabled={busy !== null || armed}
          onClick={() => run("arm", api.arm, { optimistic: { armed: true } })}>
          {label("arm", "Arm")}
        </button>
        <button className="btn" disabled={busy !== null || !armed}
          onClick={() => run("disarm", api.disarm, { optimistic: { armed: false } })}>
          {label("disarm", "Disarm")}
        </button>
        <button className={`btn ${stop ? "warn" : ""}`} disabled={busy !== null}
          onClick={() => run("stop", () => api.stopTrading(!stop), { optimistic: { stop_trading: !stop } })}>
          {label("stop", stop ? "Resume Trading" : "Stop Trading")}
        </button>
        <button className={`btn ${cl ? "" : "danger"}`} disabled={busy !== null}
          onClick={() => run("cl", () => api.confirmLive(!cl), {
            optimistic: { confirm_live: !cl },
            confirm: !cl ? "Turn Confirm Live ON? Entries can fire once armed." : undefined,
          })}>
          {label("cl", cl ? "Confirm Live: ON" : "Confirm Live: OFF")}
        </button>
      </div>

      <div className="config">
        <label>
          Stop-loss %
          <select value={pct} onChange={(e) => setPct(Number(e.target.value))}>
            {STOP_PCT_SET.map((p) => <option key={p} value={p}>{p}%</option>)}
          </select>
        </label>
        <button className="btn" disabled={busy !== null}
          onClick={() => run("config", async () => {
            await api.updateConfig({ stop_loss_pct: pct });
            setMsg({ text: `stop_loss_pct set to ${pct}%`, err: false });
          })}>
          {label("config", "Apply")}
        </button>
      </div>

      <p className={`msg ${msg?.err ? "err" : ""}`}>{msg?.text ?? ""}</p>
    </section>
  );
}
