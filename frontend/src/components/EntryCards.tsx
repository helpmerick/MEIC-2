import type { EntryCard, EntryStatus } from "../types";

// Per-entry lifecycle cards (doc 05 §8). Pure presentation — each card renders
// whatever the read model reports; no trading logic in the frontend (UI-03).

const STATUS_META: Record<EntryStatus, { label: string; cls: string; icon: string }> = {
  PENDING: { label: "Pending", cls: "s-pending", icon: "○" },
  PROTECTED: { label: "Protected", cls: "s-protected", icon: "🛡️" },
  STOPPED: { label: "Stopped", cls: "s-stopped", icon: "🔴" },
  LEX_RECOVERED: { label: "LEX recovered", cls: "s-lex", icon: "↩️" },
  EXPIRED: { label: "Expired", cls: "s-expired", icon: "⌛" },
  DECAY_CLOSED: { label: "Decay close", cls: "s-decay", icon: "📉" },
  CLOSED: { label: "Closed", cls: "s-closed", icon: "📕" },
};

function money(v: string) {
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(2);
}

function Card({ e }: { e: EntryCard }) {
  const meta = STATUS_META[e.status] ?? STATUS_META.PENDING;
  const pnl = Number(e.pnl);
  return (
    <div className={`entry-card ${meta.cls}`}>
      <div className="ec-top">
        <span className="ec-id">{e.entry_id}</span>
        <span className={`ec-badge ${meta.cls}`}>{meta.icon} {meta.label}</span>
      </div>
      <div className="ec-pnl">
        <span className={pnl >= 0 ? "pos" : "neg"}>{money(e.pnl)}</span>
      </div>
      <div className="ec-meta">
        <span>credit ${Number(e.net_credit).toFixed(2)}</span>
        {e.sides_stopped.length > 0 && <span className="tag stop">{e.sides_stopped.join("+")} stopped</span>}
        {e.recovered && <span className="tag lex">LEX</span>}
        {e.sides_expired.length > 0 && <span className="tag exp">{e.sides_expired.length} exp</span>}
      </div>
    </div>
  );
}

export function EntryCards({ entries }: { entries: EntryCard[] }) {
  return (
    <section className="card">
      <div className="row between">
        <h2>Entries</h2>
        <span className="muted">{entries.length} armed</span>
      </div>
      {entries.length === 0 ? (
        <p className="muted">No entries yet — waiting for the first window to fill.</p>
      ) : (
        <div className="entry-grid">
          {entries.map((e) => <Card key={e.entry_id} e={e} />)}
        </div>
      )}
    </section>
  );
}
