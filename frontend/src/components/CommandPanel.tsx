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
  const [liveModal, setLiveModal] = useState(false);   // the type-LIVE gate

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
  const live = state?.trading_mode === "live";
  const label = (n: string, t: string) => (busy === n ? <><span className="spin" />{t}</> : t);

  // Turning Confirm Live ON is the deliberate "ready to trade live" gate (ENT-01b).
  // ON => a typed-LIVE modal (real money demands more than an OK). OFF => instant,
  // because DISABLING a safety gate should never have friction.
  function toggleConfirmLive() {
    if (cl) {
      void run("cl", () => api.confirmLive(false), { optimistic: { confirm_live: false } });
    } else {
      setLiveModal(true);
    }
  }

  async function confirmLiveOn() {
    setLiveModal(false);
    await run("cl", () => api.confirmLive(true), { optimistic: { confirm_live: true } });
  }

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
          onClick={toggleConfirmLive}>
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

      {liveModal && (
        <ConfirmLiveModal
          live={live}
          onCancel={() => setLiveModal(false)}
          onConfirm={() => void confirmLiveOn()}
        />
      )}
    </section>
  );
}

// Type LIVE to arm the real-money gate (ENT-01b Confirm Live). Requires the exact
// word — Enter or the button submits; anything else is refused. What "live" MEANS
// is set by which process you launched: in `live_app` this readies REAL orders; in
// the paper demo it toggles the simulator, because that process has no live broker.
export function ConfirmLiveModal({
  live, onCancel, onConfirm,
}: {
  live: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [text, setText] = useState("");
  const ok = text.trim().toUpperCase() === "LIVE";

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Confirm live trading">
      <div className="modal">
        <h3>Ready the bot to trade live?</h3>
        <p className="sub">
          Turning Confirm Live ON is one of the three switches that let entries fire
          (ARMED ∧ Confirm Live ∧ Stop Trading off).
        </p>

        {live ? (
          <p className="msg live-banner" role="alert">
            This session is <strong>LIVE</strong> — real money. Once armed, entries can place real orders.
          </p>
        ) : (
          <p className="msg" role="status">
            This session is <strong>PAPER</strong> (simulator). This readies paper trading only —
            real money requires launching the live app.
          </p>
        )}

        <label className="field" style={{ marginTop: 12 }}>
          <span>Type LIVE to confirm</span>
          <input
            aria-label="type LIVE to confirm"
            autoFocus
            value={text}
            placeholder="LIVE"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && ok) onConfirm(); }}
          />
        </label>

        <div className="modal-actions">
          <button className="btn" onClick={onCancel}>Cancel</button>
          <button className="btn primary" disabled={!ok} onClick={onConfirm}>
            Confirm Live
          </button>
        </div>
      </div>
    </div>
  );
}
