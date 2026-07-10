import { useEffect, useState } from "react";
import { api } from "../../api";
import type { DayStatus, EntryCard, ReportSummary, ScheduleView } from "../../types";
import { NextEntryCountdown } from "../NextEntryCountdown";
import { dollars, TrustBadge } from "./shared";

const TERMINAL = ["CLOSED", "EXPIRED", "DECAY_CLOSED"];

/** Fraction of the ceiling the day's SCHEDULED worst case already consumes —
 * mirrors SchedulePanel's usedFraction (same meter, same source: /schedule). */
function usedFraction(view: ScheduleView | null): number | null {
  if (!view || view.max_day_risk === null) return null;
  const ceiling = Number(view.max_day_risk);
  if (!(ceiling > 0)) return null;
  return Number(view.day_total_estimate) / ceiling;
}

// UI-26 band ① — today's net so far, fired/remaining + the UI-24 countdown,
// and the trading page's risk-used vs max_day_risk meter, reused verbatim.
export function TodayHero({ entries }: { entries: EntryCard[] }) {
  const [summary, setSummary] = useState<ReportSummary | null>(null);
  const [schedule, setSchedule] = useState<ScheduleView | null>(null);
  const [dayStatus, setDayStatus] = useState<DayStatus | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([api.getReportSummary({ period: "today" }), api.getSchedule(), api.getDayStatus()])
      .then(([s, sched, ds]) => {
        if (!alive) return;
        setSummary(s);
        setSchedule(sched);
        setDayStatus(ds);
      })
      .catch(() => {
        /* leave last-known values in place; a manual period-picker refresh will retry */
      });
    return () => {
      alive = false;
    };
  }, []);

  // Live unrealized mark (live mode only — paper never carries live_pnl, FEATURE 3):
  // shown as its OWN figure, never combined with core.net_pnl into a derived
  // "split" (that would mix a per-share/live-mark scale into a dollarized
  // period total — exactly the re-derivation UI-03 forbids).
  const openEntries = entries.filter((e) => !TERMINAL.includes(e.status));
  const liveMarks = openEntries.filter((e): e is EntryCard & { live_pnl: string } => e.live_pnl != null);
  const unrealizedSum = liveMarks.length
    ? liveMarks.reduce((sum, e) => sum + Number(e.live_pnl), 0)
    : null;

  const used = usedFraction(schedule);
  const meterClass = schedule?.exceeds_max_day_risk ? "over" : used !== null && used > 0.8 ? "warn" : "";

  if (!summary) {
    return (
      <section className="card today-hero">
        <h2>Today</h2>
        <p className="muted">Loading…</p>
      </section>
    );
  }

  return (
    <section className="card today-hero" data-testid="today-hero">
      <div className="card-head">
        <h2>Today</h2>
        <TrustBadge trust={summary.trust} />
      </div>

      <div className="stat-row">
        <div className="stat hero-stat">
          <span className="stat-label">Net so far (bot-computed)</span>
          <span className={`stat-val ${Number(summary.core.net_pnl) >= 0 ? "pos" : "neg"}`}>
            {dollars(summary.core.net_pnl)}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Unrealized (live mark)</span>
          <span className={`stat-val ${unrealizedSum === null ? "" : "unrealized"}`}>
            {unrealizedSum === null ? "—" : `${unrealizedSum >= 0 ? "+" : "-"}$${Math.abs(unrealizedSum).toFixed(2)}`}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Fired</span>
          <span className="stat-val">{dayStatus?.filled ?? summary.core.filled}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Remaining</span>
          <span className="stat-val">{dayStatus?.entries_remaining ?? "—"}</span>
        </div>
      </div>

      <NextEntryCountdown />

      {schedule && (
        <div className="risk-readout" data-testid="today-risk-gauge">
          <div className="risk-line">
            <span className="k">Risk used (scheduled worst-case)</span>
            <span className={`v ${schedule.exceeds_max_day_risk ? "over" : ""}`}>
              ${schedule.day_total_estimate}
              {schedule.max_day_risk ? ` / $${schedule.max_day_risk}` : ""}
            </span>
          </div>
          <div className={`meter ${meterClass}`} aria-hidden>
            <i style={{ width: `${Math.min(100, Math.max(0, (used ?? 0) * 100))}%` }} />
          </div>
          <div className="risk-line">
            <span className="k">Headroom</span>
            <span
              className={`v ${schedule.headroom === null ? "none" : schedule.exceeds_max_day_risk ? "over" : ""}`}
            >
              {schedule.headroom === null ? "no ceiling set" : `$${schedule.headroom}`}
            </span>
          </div>
        </div>
      )}
    </section>
  );
}
