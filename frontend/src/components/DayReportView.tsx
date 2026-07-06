import type { DayReport } from "../types";

// Read-only day report (EOD-05), projected from the event log by the backend.
// Every figure is bot-computed and deterministic (PNL-03).

function money(v: string) {
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(2);
}

export function DayReportView({ report }: { report: DayReport | null }) {
  if (!report) return <section className="card"><h2>Day report</h2><p className="muted">…</p></section>;
  const pnl = Number(report.day_pnl);
  return (
    <section className="card">
      <div className="row between">
        <h2>Day report</h2>
        <span className="muted">{report.date ?? "—"}</span>
      </div>

      <div className="stat-row">
        <div className="stat"><span className="stat-label">Filled</span><span className="stat-val">{report.entries_filled}</span></div>
        <div className="stat"><span className="stat-label">Stops</span><span className="stat-val">{report.stops_hit}</span></div>
        <div className="stat"><span className="stat-label">LEX</span><span className="stat-val">{report.lex_recoveries}</span></div>
        <div className="stat"><span className="stat-label">Decay</span><span className="stat-val">{report.decay_closes}</span></div>
        <div className="stat"><span className="stat-label">Credit</span><span className="stat-val">{money(report.total_credit)}</span></div>
        <div className="stat"><span className="stat-label">Day P&L</span>
          <span className={`stat-val ${pnl >= 0 ? "pos" : "neg"}`}>{money(report.day_pnl)}</span></div>
      </div>

      {Object.keys(report.per_entry_pnl).length > 0 && (
        <table className="entries">
          <thead><tr><th>Entry</th><th>P&L</th></tr></thead>
          <tbody>
            {Object.entries(report.per_entry_pnl).map(([id, v]) => (
              <tr key={id}><td>{id}</td>
                <td className={Number(v) >= 0 ? "pos" : "neg"}>{money(v)}</td></tr>
            ))}
          </tbody>
        </table>
      )}

      {report.skips.length > 0 && (
        <div className="skips">
          <h3>Skips</h3>
          <ul>{report.skips.map(([n, reason], i) => <li key={i}>entry {n}: <code>{reason}</code></li>)}</ul>
        </div>
      )}
    </section>
  );
}
