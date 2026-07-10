import type { DaySlippageFamilies } from "../../types";
import { GapNote, plainDollars } from "./shared";

// RPT-06 (slippage in) / RPT-07 (slippage out, four families). Only the
// stop-outs family is exposed, and only at the single-DAY grain
// (/reports/day/{date}) — /reports/summary has no period-aggregated
// slippage figures at all in this slice. Every other family renders an
// honest "not yet captured" state (UI-26), never a fabricated number.
export function SlippagePanels({ daySlippage }: { daySlippage?: DaySlippageFamilies }) {
  return (
    <div className="slippage-panels" data-testid="slippage-panels">
      <div className="slip-family">
        <h4>RPT-06 — Slippage in</h4>
        <GapNote>
          Not yet captured by the API at any scope — fill credit vs first-rung credit isn't
          surfaced by /reports/summary or /reports/day in this slice.
        </GapNote>
      </div>

      <div className="slip-family">
        <h4>RPT-07 — Stop-outs</h4>
        {daySlippage ? (
          daySlippage.stop_outs.n > 0 ? (
            <table className="entries" data-testid="stop-out-table">
              <thead>
                <tr>
                  <th>n</th>
                  <th>mean</th>
                  <th>p50</th>
                  <th>p90</th>
                  <th>max</th>
                  <th>mean ticks</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>{daySlippage.stop_outs.n}</td>
                  <td>{plainDollars(daySlippage.stop_outs.mean)}</td>
                  <td>{plainDollars(daySlippage.stop_outs.p50)}</td>
                  <td>{plainDollars(daySlippage.stop_outs.p90)}</td>
                  <td>{plainDollars(daySlippage.stop_outs.max)}</td>
                  <td>{daySlippage.stop_outs.mean_ticks ?? "—"}</td>
                </tr>
              </tbody>
            </table>
          ) : (
            <p className="muted">No stop-outs this day.</p>
          )
        ) : (
          <GapNote>
            Only available per-day — open a day's drill-down. /reports/summary has no
            period-aggregated RPT-07 figures in this slice.
          </GapNote>
        )}
      </div>

      <div className="slip-family">
        <h4>RPT-07 — Long recovery / closes / decay buybacks</h4>
        <GapNote>
          Not yet captured: the event schema doesn't record mark-at-stop/target-price capture
          for these three families yet (a documented slice-2 gap in reports.py itself).
        </GapNote>
      </div>
    </div>
  );
}
