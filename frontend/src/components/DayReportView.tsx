import { useEffect, useRef } from "react";
import type { DayReport } from "../types";

function money(v: string) {
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(2);
}

// flashes when its value changes — cheap "something happened" feedback
function Stat({ label, value, cls, hero }: { label: string; value: string | number; cls?: string; hero?: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);
  const prev = useRef(value);
  useEffect(() => {
    if (prev.current !== value && ref.current) {
      ref.current.classList.remove("flash");
      void ref.current.offsetWidth; // restart animation
      ref.current.classList.add("flash");
      prev.current = value;
    }
  }, [value]);
  return (
    <div className={`stat ${hero ? "hero-stat" : ""}`}>
      <span className="stat-label">{label}</span>
      <span ref={ref} className={`stat-val ${cls ?? ""}`}>{value}</span>
    </div>
  );
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
        <Stat label="Filled" value={report.entries_filled} />
        <Stat label="Stops" value={report.stops_hit} />
        <Stat label="LEX" value={report.lex_recoveries} />
        <Stat label="Decay" value={report.decay_closes} />
        <Stat label="Credit" value={money(report.total_credit)} />
        <Stat label="Day P&L" value={money(report.day_pnl)} cls={pnl >= 0 ? "pos" : "neg"} hero />
      </div>

      {Object.keys(report.per_entry_pnl).length > 0 && (
        <table className="entries">
          <thead><tr><th>Entry</th><th>P&amp;L</th></tr></thead>
          <tbody>
            {Object.entries(report.per_entry_pnl).map(([id, v]) => (
              <tr key={id}>
                <td>{id}</td>
                <td className={Number(v) >= 0 ? "pos" : "neg"}>{money(v)}</td>
              </tr>
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
