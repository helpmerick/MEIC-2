import { useEffect, useRef } from "react";
import type { DayReport, EntryCard } from "../types";
import { contractDollars, contractDollarsValue, formatDollars } from "../money";

// SPX options carry a $100 multiplier and ENT-04 lets an entry trade more
// than one contract (operator request 2026-07-11). `report`'s Decimal
// fields (total_credit, day_pnl, per_entry_pnl) are PER-SHARE and NOT
// contracts-scaled — they are plain sums of each entry's own per-share
// net_credit/pnl (backend/src/meic/domain/projection.py `day_report`), with
// no per-entry contracts breakdown of their own. `entries` (the SAME
// snapshot, same event-log fold — see useLiveBot.ts's `applySnapshot`) is
// threaded in here only to read each entry's own contracts count off its
// legs, then the credit/day-P&L tiles are recomputed from `entries` directly
// (summing each entry's OWN contract-dollar amount) rather than scaling
// `report.total_credit`/`day_pnl` by one flat multiplier, which would
// misscale the moment two entries on the same day carry different
// contracts counts.
function contractsOf(e: EntryCard | undefined): number {
  return e?.legs && e.legs.length > 0 ? e.legs[0].qty : 1;
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

export function DayReportView({ report, entries }: { report: DayReport | null; entries: EntryCard[] }) {
  if (!report) return <section className="card"><h2>Day report</h2><p className="muted">…</p></section>;
  const byId = new Map(entries.map((e) => [e.entry_id, e]));
  const totalCreditDollars = entries.reduce(
    (sum, e) => sum + contractDollarsValue(e.net_credit, contractsOf(e)), 0);
  const dayPnlDollars = entries.reduce(
    (sum, e) => sum + contractDollarsValue(e.pnl, contractsOf(e)), 0);
  return (
    <section className="card">
      <div className="card-head">
        <h2>Day report</h2>
        <span className="muted">{report.date ?? "—"}</span>
      </div>

      <div className="stat-row">
        <Stat label="Filled" value={report.entries_filled} />
        <Stat label="Stops" value={report.stops_hit} />
        <Stat label="LEX" value={report.lex_recoveries} />
        <Stat label="Decay" value={report.decay_closes} />
        <Stat label="Credit" value={formatDollars(totalCreditDollars)} />
        <Stat label="Day P&L" value={formatDollars(dayPnlDollars)} cls={dayPnlDollars >= 0 ? "pos" : "neg"} hero />
      </div>

      {Object.keys(report.per_entry_pnl).length > 0 && (
        <table className="entries">
          <thead><tr><th>Entry</th><th>P&amp;L</th></tr></thead>
          <tbody>
            {Object.entries(report.per_entry_pnl).map(([id, v]) => (
              <tr key={id}>
                <td>{id}</td>
                <td className={Number(v) >= 0 ? "pos" : "neg"}>{contractDollars(v, contractsOf(byId.get(id)))}</td>
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
