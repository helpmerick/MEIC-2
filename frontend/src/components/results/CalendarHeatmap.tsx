import { useState } from "react";
import type { DailyRow } from "../../types";
import { resultsDayHref } from "../../router";
import { signClass } from "./shared";

// RPT-09 daily P&L calendar heatmap. Cells for days with no entry attempt
// (RPT-01: disarmed flat days are excluded by construction) render as an
// honest "idle" cell, never a fabricated zero. Weeks run Monday->Sunday;
// Saturday/Sunday get their own "weekend" treatment, visually distinct from
// "idle" (a weekend never had a session at all — it is not a disarmed
// trading day, see the legend). Every month the daily series spans renders
// side by side (oldest -> newest) in one horizontally scrollable strip so
// the page itself never scrolls sideways.

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function monthKey(date: string): string {
  return date.slice(0, 7);
}

function daysInMonth(month: string): number {
  const [y, m] = month.split("-").map(Number);
  return new Date(y, m, 0).getDate();
}

// Monday-first weekday index: 0=Mon .. 6=Sun (JS's native getDay() is 0=Sun..6=Sat).
function mondayIndex(iso: string): number {
  const jsDay = new Date(`${iso}T00:00:00`).getDay();
  return (jsDay + 6) % 7;
}

interface Cell {
  date: string | null; // null = leading filler (before the 1st's weekday)
  row: DailyRow | null;
  weekend: boolean;
}

// The screen-reader label: entries/wins/losses/total P&L for the day (null
// counts = a broker-imported day, RPT-16 — described honestly, never a
// fabricated 0-0). The visual hover box below renders the same facts.
function cellLabel(row: DailyRow): string {
  const n = Number(row.net_pnl);
  const pnlText = `${n >= 0 ? "+" : ""}${row.net_pnl}`;
  if (row.wins === null || row.losses === null) {
    return `${row.date}: ${pnlText} (${row.trust}) — win/loss breakdown not applicable (broker-imported)`;
  }
  return `${row.date}: ${row.entries ?? 0} entries, ${row.wins}W / ${row.losses}L, ${pnlText} (${row.trust})`;
}

// The hover box's middle line — UI-26a (v1.61): date, net $, ENTRIES,
// wins/losses. All three counts come from the backend's ONE aggregation path
// (RPT-16 honesty: an imported day has no entry-level outcome to count — say
// so, never render "0 entries · 0 wins · 0 losses").
function winLossLine(row: DailyRow): string {
  if (row.wins === null || row.losses === null) {
    return "win/loss breakdown not applicable (broker-imported)";
  }
  return `${row.entries ?? 0} entries · ${row.wins} wins · ${row.losses} losses`;
}

interface TipState {
  row: DailyRow;
  x: number; // viewport px, cell's horizontal center
  y: number; // viewport px, cell's top edge
}

export function CalendarHeatmap({ daily }: { daily: DailyRow[] }) {
  // Hover box for trading-day cells. position:fixed (viewport coordinates
  // from getBoundingClientRect) so it ESCAPES the .heatmap-strip
  // overflow-x:auto scroll container — a CSS-only child tooltip would be
  // clipped at the strip's edges (top row, last visible column).
  const [tip, setTip] = useState<TipState | null>(null);

  if (daily.length === 0) {
    return <p className="muted" data-testid="heatmap-empty">No trading days in this period yet.</p>;
  }

  const showTip = (row: DailyRow) => (e: React.MouseEvent | React.FocusEvent) => {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setTip({ row, x: r.left + r.width / 2, y: r.top });
  };
  const hideTip = () => setTip(null);

  const byMonth = new Map<string, Map<string, DailyRow>>();
  for (const d of daily) {
    const k = monthKey(d.date);
    if (!byMonth.has(k)) byMonth.set(k, new Map());
    byMonth.get(k)!.set(d.date, d);
  }
  const maxAbs = Math.max(1, ...daily.map((d) => Math.abs(Number(d.net_pnl))));

  return (
    <div className="heatmap" data-testid="calendar-heatmap">
      <div className="heatmap-strip" data-testid="heatmap-strip">
        {[...byMonth.entries()].map(([month, byDate]) => {
          const firstIdx = mondayIndex(`${month}-01`);
          const total = daysInMonth(month);
          const cells: Cell[] = [
            ...Array.from({ length: firstIdx }, () => ({ date: null, row: null, weekend: false })),
            ...Array.from({ length: total }, (_, i) => {
              const iso = `${month}-${String(i + 1).padStart(2, "0")}`;
              const weekdayIdx = (firstIdx + i) % 7;
              return { date: iso, row: byDate.get(iso) ?? null, weekend: weekdayIdx >= 5 };
            }),
          ];
          return (
            <div key={month} className="heatmap-month" data-testid={`heatmap-month-${month}`}>
              <div className="heatmap-month-label">{month}</div>
              <div className="heatmap-weekday-row" aria-hidden="true">
                {WEEKDAY_LABELS.map((w) => (
                  <span key={w} className="heatmap-weekday">{w}</span>
                ))}
              </div>
              <div className="heatmap-grid">
                {cells.map((cell, i) => {
                  if (cell.date === null) {
                    return <span key={`filler-${i}`} className="heatmap-cell filler" aria-hidden />;
                  }
                  // Real data always wins over decorative weekend styling —
                  // SPX never trades a weekend, but honesty over decoration
                  // if a row ever did show up on one.
                  if (cell.row) {
                    const n = Number(cell.row.net_pnl);
                    const cls = n > 0 ? "gain" : n < 0 ? "loss" : "flat";
                    const intensity = Math.min(1, Math.abs(n) / maxAbs);
                    return (
                      <a
                        key={cell.date}
                        href={resultsDayHref(cell.date)}
                        className={`heatmap-cell heatmap-cell-link ${cls}`}
                        style={{ opacity: n === 0 ? 1 : 0.25 + intensity * 0.75 }}
                        aria-label={cellLabel(cell.row)}
                        onMouseEnter={showTip(cell.row)}
                        onMouseLeave={hideTip}
                        onFocus={showTip(cell.row)}
                        onBlur={hideTip}
                      />
                    );
                  }
                  if (cell.weekend) {
                    return (
                      <span
                        key={cell.date}
                        className="heatmap-cell weekend"
                        title={`${cell.date}: weekend`}
                        aria-label={`${cell.date}, weekend, no session`}
                      />
                    );
                  }
                  return (
                    <span
                      key={cell.date}
                      className="heatmap-cell idle"
                      title={`${cell.date}: no trading day`}
                      aria-label={`${cell.date}, no trading day`}
                    />
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
      {tip && (
        <div
          className="heatmap-tooltip"
          data-testid="heatmap-tooltip"
          role="tooltip"
          style={{ left: tip.x, top: tip.y }}
        >
          <div className="heatmap-tooltip-date">{tip.row.date}</div>
          <div className="heatmap-tooltip-wl">{winLossLine(tip.row)}</div>
          <div className={`heatmap-tooltip-pnl ${signClass(tip.row.net_pnl)}`}>
            {Number(tip.row.net_pnl) >= 0 ? "+" : ""}
            {tip.row.net_pnl}
          </div>
        </div>
      )}
      <div className="heatmap-legend">
        <span className="heatmap-cell gain" /> gain
        <span className="heatmap-cell loss" /> loss
        <span className="heatmap-cell flat" /> flat
        <span className="heatmap-cell idle" /> no trading day
        <span className="heatmap-cell weekend" /> weekend
      </div>
    </div>
  );
}
