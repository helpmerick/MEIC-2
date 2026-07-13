import { useState } from "react";
import type { EntryCard, EntryStatus } from "../types";
import { contractDollars, contractDollarsPlain } from "../money";

// Per-entry lifecycle cards (doc 05 §8). Presentation + the operator's instant
// Close action (UI-16); all validation stays server-side (UI-03).

const STATUS_META: Record<EntryStatus, { label: string; cls: string; icon: string }> = {
  PENDING: { label: "Pending", cls: "s-pending", icon: "○" },
  PROTECTED: { label: "Protected", cls: "s-protected", icon: "🛡️" },
  STOPPED: { label: "Stopped", cls: "s-stopped", icon: "🔴" },
  LEX_RECOVERED: { label: "LEX recovered", cls: "s-lex", icon: "↩️" },
  EXPIRED: { label: "Expired", cls: "s-expired", icon: "⌛" },
  DECAY_CLOSED: { label: "Decay close", cls: "s-decay", icon: "📉" },
  CLOSED: { label: "Closed", cls: "s-closed", icon: "📕" },
};

const TERMINAL: EntryStatus[] = ["CLOSED", "EXPIRED", "DECAY_CLOSED"];

// ENT-04: each entry carries its own contracts count, read off any leg's
// filled quantity (ORD-01: all four legs fill balanced, so any one leg's qty
// is the entry's contracts) — the same source backend/src/meic/reporting/
// folds.py's `contracts_of` reads (`entry.legs[0].qty`). An entry with no
// recorded legs (never filled) defaults to 1; harmless, since every dollar
// figure it feeds is 0 anyway (no fill, no premium) — never load-bearing.
function entryContracts(e: EntryCard): number {
  return e.legs && e.legs.length > 0 ? e.legs[0].qty : 1;
}

// FEATURE 1: the fill time as the operator's LOCAL wall-clock. `placed_at` is
// an ISO string with an offset, so the browser resolves it correctly on its own.
function placedTime(placedAt: string | null | undefined): string | null {
  if (!placedAt) return null;
  return new Date(placedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// FEATURE 2: "P 7535/7510 +$172" — short/long strike and the per-side premium
// in real contract dollars (operator request 2026-07-11: premium x100 x the
// entry's contracts — "—" when the broker reported no allocation for one of
// the two legs).
function legLine(e: EntryCard, side: "PUT" | "CALL", contracts: number): string | null {
  const legs = e.legs;
  if (!legs || legs.length === 0) return null;
  const short = legs.find((l) => l.side === side && l.role === "short");
  const long = legs.find((l) => l.side === side && l.role === "long");
  if (!short || !long) return null;
  const label = side === "PUT" ? "P" : "C";
  const premium = e.premium_received?.[side];
  return `${label} ${short.strike}/${long.strike} ${premium != null ? contractDollars(premium, contracts) : "—"}`;
}

// FEATURE 3: "P/L $+123 (as of HH:MM)", green when >= 0 / red when < 0, "—"
// when the live estimate is unavailable (paper, a stale snapshot, or a mark
// outside the ATM band — never a fabricated number).
function livePnl(e: EntryCard): { text: string; cls: string } {
  if (e.live_pnl == null) return { text: "—", cls: "" };
  const n = Number(e.live_pnl);
  const asof = e.live_pnl_asof
    ? new Date(e.live_pnl_asof).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;
  const amount = (n >= 0 ? "+" : "") + Math.round(n);
  return { text: `P/L $${amount}${asof ? ` (as of ${asof})` : ""}`, cls: n >= 0 ? "pos" : "neg" };
}

// v1.58 TPF/TPT: profit% "—" when unavailable (paper, stale/no snapshot).
function profitPct(e: EntryCard): string {
  if (e.profit_pct == null) return "—";
  const n = Number(e.profit_pct);
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)}%`;
}

// TPF/TPT exit controls: an operator-typed level + Set/Clear, server-side
// gap validation is authoritative (UI-03/UI-15) — this control does no
// client-side selection logic, it only surfaces the server's verdict.
function ExitControl({ label, armed, title, disabled, onSet, onClear }: {
  label: string;
  armed: number | null | undefined;
  title: string;
  disabled?: boolean;
  onSet: (level: number) => Promise<void>;
  onClear: () => Promise<void>;
}) {
  const [level, setLevel] = useState("");
  const [busy, setBusy] = useState(false);

  async function handleSet() {
    const n = Number(level);
    if (!Number.isFinite(n) || level.trim() === "") return;
    setBusy(true);
    try { await onSet(n); } finally { setBusy(false); }
  }

  async function handleClear() {
    setBusy(true);
    try { await onClear(); } finally { setBusy(false); }
  }

  return (
    <div className="ec-exit-control" title={title}>
      <span className="ec-exit-label">{label}{armed != null ? `: ${armed}%` : ": —"}</span>
      {!disabled && (
        <span className="ec-exit-inputs">
          <input
            type="number" step={5} min={5} max={95} placeholder="%"
            aria-label={`${label} level`} value={level} disabled={busy}
            onChange={(ev) => setLevel(ev.target.value)}
          />
          <button type="button" onClick={handleSet} disabled={busy}>
            {armed != null ? "Adjust" : "Set"}
          </button>
          {armed != null && (
            <button type="button" onClick={handleClear} disabled={busy}>Clear</button>
          )}
        </span>
      )}
    </div>
  );
}

function Card({ e, onClose, onSetFloor, onClearFloor, onSetTarget, onClearTarget }: {
  e: EntryCard;
  onClose: (id: string) => Promise<void>;
  onSetFloor?: (id: string, level: number) => Promise<void>;
  onClearFloor?: (id: string) => Promise<void>;
  onSetTarget?: (id: string, level: number) => Promise<void>;
  onClearTarget?: (id: string) => Promise<void>;
}) {
  const meta = STATUS_META[e.status] ?? STATUS_META.PENDING;
  const pnl = Number(e.pnl);
  const contracts = entryContracts(e);
  const [busy, setBusy] = useState(false);
  const closeable = !TERMINAL.includes(e.status);
  // UC-14/CLS-03: a PENDING/WORKING entry has nothing filled to close — the
  // same button is the instant "Cancel entry" (backend routes it to the
  // CLS-03 cancel; the UI has no close logic of its own, CLS-02).
  const pending = e.status === "PENDING";
  const placed = placedTime(e.placed_at);
  const putLine = legLine(e, "PUT", contracts);
  const callLine = legLine(e, "CALL", contracts);
  const live = livePnl(e);
  const exitControlsAvailable = closeable && onSetFloor && onClearFloor && onSetTarget && onClearTarget;

  async function handleClose() {
    setBusy(true);
    try { await onClose(e.entry_id); } finally { setBusy(false); }
  }

  return (
    <div className={`entry-card ${meta.cls}`}>
      <div className="ec-top">
        <span className="ec-id">{e.entry_id}</span>
        <span className={`ec-badge ${meta.cls}`}>{meta.icon} {meta.label}</span>
      </div>
      <div className="ec-pnl">
        <span className={pnl >= 0 ? "pos" : "neg"}>{contractDollars(e.pnl, contracts)}</span>
      </div>
      <div className="ec-meta">
        <span>credit ${contractDollarsPlain(e.net_credit, contracts)}</span>
        {/* Live mark-to-market P&L sits RIGHT BESIDE the premium received, big and
            green/red (operator request 2026-07-13): it is the number that says how
            the trade is actually doing NOW, so it must not be buried in small grey
            text below the legs. `.ec-livepnl` deliberately sets NO colour — that
            lets `.pos`/`.neg` win (they were previously cancelled by an
            `.ec-livepnl { color: var(--faint) }` of equal specificity declared
            later, which is why this always rendered grey). */}
        <span className={`ec-livepnl ${live.cls}`}>{live.text}</span>
        {e.sides_stopped.length > 0 && <span className="tag stop">{e.sides_stopped.join("+")} stopped</span>}
        {e.recovered && <span className="tag lex">LEX</span>}
        {e.sides_expired.length > 0 && <span className="tag exp">{e.sides_expired.length} exp</span>}
        {e.settlement_pending && (
          <span className="tag pending" title="Broker settlement not yet captured (EOD-01)">
            provisional — settlement pending
          </span>
        )}
      </div>
      {placed && <div className="ec-placed">Placed {placed}</div>}
      {(putLine || callLine) && (
        <div className="ec-legs">
          {putLine && <div className="ec-leg">{putLine}</div>}
          {callLine && <div className="ec-leg">{callLine}</div>}
        </div>
      )}
      <div className="ec-profitpct">Profit: {profitPct(e)}</div>
      {exitControlsAvailable && (
        <div className="ec-exits">
          <ExitControl
            label="Floor" armed={e.tpf_floor ?? null}
            title="TPF: bot-side only — active only while the bot is running (UI-15)"
            onSet={(level) => onSetFloor!(e.entry_id, level)}
            onClear={() => onClearFloor!(e.entry_id)}
          />
          <ExitControl
            label="Target" armed={e.tpt_disarmed ? null : (e.tpt_target ?? null)}
            title="TPT: bot-side only — active only while the bot is running (TPT-04)"
            disabled={e.tpt_disarmed === true}
            onSet={(level) => onSetTarget!(e.entry_id, level)}
            onClear={() => onClearTarget!(e.entry_id)}
          />
          {e.tpt_disarmed && (
            <span className="tag disarmed" title="A stop already filled on this entry (TPT-05)">
              target disarmed
            </span>
          )}
          {!e.tpt_disarmed && e.tpt_feedback && (
            <div className="ec-tpt-feedback">
              Exit armed: closes at debit ≤ ${e.tpt_feedback.debit} (keep ≥ ${e.tpt_feedback.keep})
            </div>
          )}
        </div>
      )}
      {closeable && (
        <button className="ec-close" onClick={handleClose} disabled={busy}
                title={pending
                  ? "Cancel this entry's working order now (CLS-03, no confirmation)"
                  : "Close this entry now (no confirmation, UI-16)"}>
          {busy ? <span className="spin" /> : null}
          {busy ? (pending ? "Cancelling…" : "Closing…") : (pending ? "Cancel entry" : "Close")}
        </button>
      )}
    </div>
  );
}

export function EntryCards({ entries, onClose, onSetFloor, onClearFloor, onSetTarget, onClearTarget }: {
  entries: EntryCard[];
  onClose: (id: string) => Promise<void>;
  // v1.58 TPF/TPT (UI-13/14/15): optional — a caller with no wiring (e.g. an
  // older harness) simply gets no exit controls rendered, same pattern as
  // PanelCommands.close() being the only always-required handler.
  onSetFloor?: (id: string, level: number) => Promise<void>;
  onClearFloor?: (id: string) => Promise<void>;
  onSetTarget?: (id: string, level: number) => Promise<void>;
  onClearTarget?: (id: string) => Promise<void>;
}) {
  return (
    <section className="card">
      <div className="card-head">
        <h2>Entries</h2>
        <span className="muted">{entries.length} armed</span>
      </div>
      {entries.length === 0 ? (
        <p className="muted">No entries yet — waiting for the first window to fill.</p>
      ) : (
        <div className="entry-grid">
          {entries.map((e) => (
            <Card key={e.entry_id} e={e} onClose={onClose}
                  onSetFloor={onSetFloor} onClearFloor={onClearFloor}
                  onSetTarget={onSetTarget} onClearTarget={onClearTarget} />
          ))}
        </div>
      )}
    </section>
  );
}
