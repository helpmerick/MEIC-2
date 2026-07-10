import type { DailyRow } from "../../types";

// RPT-09 daily P&L calendar heatmap. Cells for days with no entry attempt
// (RPT-01: disarmed flat days are excluded by construction) render as an
// honest "idle" cell, never a fabricated zero.

function monthKey(date: string): string {
  return date.slice(0, 7);
}

function daysInMonth(month: string): number {
  const [y, m] = month.split("-").map(Number);
  return new Date(y, m, 0).getDate();
}

function weekdayOf(iso: string): number {
  return new Date(`${iso}T00:00:00`).getDay(); // 0=Sun..6=Sat — grid layout only
}

interface Cell {
  date: string | null; // null = leading filler (before the 1st's weekday)
  row: DailyRow | null;
}

export function CalendarHeatmap({ daily }: { daily: DailyRow[] }) {
  if (daily.length === 0) {
    return <p className="muted" data-testid="heatmap-empty">No trading days in this period yet.</p>;
  }

  const byMonth = new Map<string, Map<string, DailyRow>>();
  for (const d of daily) {
    const k = monthKey(d.date);
    if (!byMonth.has(k)) byMonth.set(k, new Map());
    byMonth.get(k)!.set(d.date, d);
  }
  const maxAbs = Math.max(1, ...daily.map((d) => Math.abs(Number(d.net_pnl))));

  return (
    <div className="heatmap" data-testid="calendar-heatmap">
      {[...byMonth.entries()].map(([month, byDate]) => {
        const firstWeekday = weekdayOf(`${month}-01`);
        const total = daysInMonth(month);
        const cells: Cell[] = [
          ...Array.from({ length: firstWeekday }, () => ({ date: null, row: null })),
          ...Array.from({ length: total }, (_, i) => {
            const iso = `${month}-${String(i + 1).padStart(2, "0")}`;
            return { date: iso, row: byDate.get(iso) ?? null };
          }),
        ];
        return (
          <div key={month} className="heatmap-month">
            <div className="heatmap-month-label">{month}</div>
            <div className="heatmap-grid">
              {cells.map((cell, i) => {
                if (cell.date === null) {
                  return <span key={`filler-${i}`} className="heatmap-cell filler" aria-hidden />;
                }
                if (!cell.row) {
                  return (
                    <span
                      key={cell.date}
                      className="heatmap-cell idle"
                      title={`${cell.date}: no trading day`}
                    />
                  );
                }
                const n = Number(cell.row.net_pnl);
                const cls = n > 0 ? "gain" : n < 0 ? "loss" : "flat";
                const intensity = Math.min(1, Math.abs(n) / maxAbs);
                return (
                  <span
                    key={cell.date}
                    className={`heatmap-cell ${cls}`}
                    style={{ opacity: n === 0 ? 1 : 0.25 + intensity * 0.75 }}
                    title={`${cell.date}: ${n >= 0 ? "+" : ""}${cell.row.net_pnl} (${cell.row.trust})`}
                  />
                );
              })}
            </div>
          </div>
        );
      })}
      <div className="heatmap-legend">
        <span className="heatmap-cell gain" /> gain
        <span className="heatmap-cell loss" /> loss
        <span className="heatmap-cell flat" /> flat
        <span className="heatmap-cell idle" /> no trading day
      </div>
    </div>
  );
}
