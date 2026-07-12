import { useEffect, useState } from "react";
import { api } from "../../api";
import type { DailyRow, MetricsResult } from "../../types";
import { dollars, GapNote, pct, plainDollars } from "./shared";

function extreme(daily: DailyRow[], pick: "max" | "min"): DailyRow | null {
  if (daily.length === 0) return null;
  return daily.reduce((best, row) => {
    const better = pick === "max"
      ? Number(row.net_pnl) > Number(best.net_pnl)
      : Number(row.net_pnl) < Number(best.net_pnl);
    return better ? row : best;
  }, daily[0]);
}

interface MonthTotal {
  month: string;
  net: string;
}

// Best/worst MONTH is fetched from the server's own per-month summary
// (core.net_pnl for month=YYYY-MM) rather than summed client-side from daily
// rows — UI-03 forbids re-deriving a money total in the frontend, and the
// backend already computes exactly this total when scoped to a month.
function useMonthTotals(daily: DailyRow[]): MonthTotal[] | null {
  const [totals, setTotals] = useState<MonthTotal[] | null>(null);
  const months = [...new Set(daily.map((d) => d.date.slice(0, 7)))].sort().join(",");

  useEffect(() => {
    let alive = true;
    const list = months ? months.split(",") : [];
    if (list.length === 0) {
      setTotals([]);
      return;
    }
    Promise.all(
      list.map((m) => api.getReportSummary({ month: m }).then((s) => ({ month: m, net: s.core.net_pnl })))
    )
      .then((rows) => {
        if (alive) setTotals(rows);
      })
      .catch(() => {
        if (alive) setTotals(null);
      });
    return () => {
      alive = false;
    };
  }, [months]);

  return totals;
}

function RecordStat({ label, date, value }: { label: string; date?: string; value?: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-val ${value ? (Number(value) >= 0 ? "pos" : "neg") : ""}`}>
        {value ? dollars(value) : "—"}
      </span>
      {date && <span className="stat-sub">{date}</span>}
    </div>
  );
}

// RPT-14 records & annotations.
export function RecordsBlock({ daily, metrics }: { daily: DailyRow[]; metrics: MetricsResult }) {
  const monthTotals = useMonthTotals(daily);
  const bestDay = extreme(daily, "max");
  const worstDay = extreme(daily, "min");
  const bestMonth =
    monthTotals && monthTotals.length
      ? monthTotals.reduce((b, m) => (Number(m.net) > Number(b.net) ? m : b))
      : null;
  const worstMonth =
    monthTotals && monthTotals.length
      ? monthTotals.reduce((w, m) => (Number(m.net) < Number(w.net) ? m : w))
      : null;
  const mdd = metrics.status === "ok" ? metrics : null;

  return (
    <div className="records-block" data-testid="records-block">
      <div className="stat-row">
        <RecordStat label="Best day" date={bestDay?.date} value={bestDay?.net_pnl} />
        <RecordStat label="Worst day" date={worstDay?.date} value={worstDay?.net_pnl} />
        <RecordStat label="Best month" date={bestMonth?.month} value={bestMonth?.net} />
        <RecordStat label="Worst month" date={worstMonth?.month} value={worstMonth?.net} />
      </div>
      <div className="stat-row">
        <div className="stat">
          <span className="stat-label">Max drawdown</span>
          <span className="stat-val neg">
            {mdd ? `${plainDollars(mdd.max_drawdown_dollars)} (${pct(mdd.max_drawdown_pct)})` : "—"}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Longest losing streak</span>
          <span className="stat-val">{mdd ? `${mdd.longest_losing_streak_days}d` : "—"}</span>
        </div>
      </div>
      <GapNote>
        Longest GREEN streak and largest single-entry win/loss aren't yet captured: RPT-04 only
        exposes a losing-streak counter (no winning-streak mirror), and per-entry P&amp;L in this
        API is a PER-SHARE value, not dollarized (only the period aggregate is) — showing either
        would mean re-deriving money client-side, which UI-03 forbids. The max-drawdown
        peak-to-trough DATE RANGE also isn't returned, only its $/% magnitude.
      </GapNote>
    </div>
  );
}
