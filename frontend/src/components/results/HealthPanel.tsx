import type { DailyRow, HealthResult } from "../../types";
import { GapNote } from "./shared";

function SkipHistogram({ histogram }: { histogram: Record<string, number> }) {
  const entries = Object.entries(histogram).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return <p className="muted">No skips this period.</p>;
  const max = Math.max(...entries.map(([, n]) => n));
  return (
    <div className="histogram" data-testid="skip-histogram">
      {entries.map(([reason, n]) => (
        <div key={reason} className="hist-row">
          <span className="hist-label">{reason}</span>
          <div className="hist-bar-track">
            <div className="hist-bar" style={{ width: `${(n / max) * 100}%` }} />
          </div>
          <span className="hist-value">{n}</span>
        </div>
      ))}
    </div>
  );
}

function Stat({ label, value, warn }: { label: string; value: number; warn?: boolean }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-val ${warn && value > 0 ? "neg" : ""}`}>{value}</span>
    </div>
  );
}

// RPT-08 operational health. `ent10_crash_alerts`/`ord08_terminal_retries` are
// `null` in this slice's API — a real gap (not journaled as replay-safe domain
// events yet) — rendered honestly, never coerced to 0.
export function HealthPanel({ health, daily }: { health: HealthResult; daily: DailyRow[] }) {
  return (
    <div className="health-panel" data-testid="health-panel">
      <div className="stat-row">
        <Stat label="Watchdog escalations" value={health.watchdog_escalations} />
        <Stat label="UNPROTECTED events" value={health.unprotected_events} warn />
        <Stat label="RSK-03 mismatches" value={health.rsk03_mismatches} warn />
        <Stat label="Corrections (RPT-15)" value={health.correction_count} warn />
      </div>

      <h4>Skip-reason histogram</h4>
      <SkipHistogram histogram={health.skip_reason_histogram} />

      <h4>ENT-10 day-task crash alerts</h4>
      {health.ent10_crash_alerts == null ? (
        <GapNote>
          Not captured as a replay-safe domain event yet — surfaced live via the Trading page's
          alert feed instead of the event log this dashboard replays.
        </GapNote>
      ) : (
        <p>{health.ent10_crash_alerts}</p>
      )}

      <h4>ORD-08 terminal retries</h4>
      {health.ord08_terminal_retries == null ? (
        <GapNote>Not captured as a replay-safe domain event yet (expected steady-state: zero).</GapNote>
      ) : (
        <p>{health.ord08_terminal_retries}</p>
      )}

      <h4>Per-day reconcile status (RPT-15)</h4>
      {daily.length === 0 ? (
        <p className="muted">No trading days in this period yet.</p>
      ) : (
        <table className="entries" data-testid="reconcile-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Trust</th>
            </tr>
          </thead>
          <tbody>
            {daily.map((d) => (
              <tr key={d.date}>
                <td>{d.date}</td>
                <td>{d.trust}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
