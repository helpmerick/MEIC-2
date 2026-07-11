import { contractDollars } from "../../money";
import type { DaySlippageFamilies } from "../../types";
import { GapNote, money } from "./shared";

// Stop-out slippage arrives as PER-SHARE price units (EC-STP-03 "fill −
// trigger" — the spec's own $ figure). Operator ruling 2026-07-11: display
// the real cash impact per contract instead (×100 via the shared exact
// converter), so a −0.10 gap reads "-$10". Ticks stay ticks.
function slipDollars(v: string | null | undefined): string {
  return v == null ? "—" : contractDollars(v);
}

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
                  <td>{slipDollars(daySlippage.stop_outs.mean)}</td>
                  <td>{slipDollars(daySlippage.stop_outs.p50)}</td>
                  <td>{slipDollars(daySlippage.stop_outs.p90)}</td>
                  <td>{slipDollars(daySlippage.stop_outs.max)}</td>
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
        <h4>RPT-07 — Long recovery</h4>
        {daySlippage ? (
          daySlippage.long_recovery.n > 0 ? (
            <>
              <table className="entries" data-testid="long-recovery-summary">
                <thead>
                  <tr>
                    <th>n</th>
                    <th>mean diff</th>
                    <th>p50</th>
                    <th>p90</th>
                    <th>max</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>{daySlippage.long_recovery.n}</td>
                    <td>{slipDollars(daySlippage.long_recovery.mean)}</td>
                    <td>{slipDollars(daySlippage.long_recovery.p50)}</td>
                    <td>{slipDollars(daySlippage.long_recovery.p90)}</td>
                    <td>{slipDollars(daySlippage.long_recovery.max)}</td>
                  </tr>
                </tbody>
              </table>
              {/* Operator ruling 2026-07-11: mark mid / realized / buffer are
                  PER-SHARE prices (matching broker records, UI-28) — plain
                  money() formatting, never x100. diff/shortfall are real
                  contract-dollar impacts, same x100 convention as stop-outs
                  above. A "—" mark/buffer cell means this row predates the
                  2026-07-11 mark-at-stop/markup stamping — honest gap, not 0. */}
              <table className="entries" data-testid="long-recovery-table">
                <thead>
                  <tr>
                    <th>entry</th>
                    <th>side</th>
                    <th>mark mid (per-share)</th>
                    <th>realized (per-share)</th>
                    <th>diff</th>
                    <th>buffer in force (per-share)</th>
                    <th>shortfall vs buffer</th>
                    <th>vs NLE estimate</th>
                  </tr>
                </thead>
                <tbody>
                  {daySlippage.long_recovery.rows.map((r, i) => (
                    <tr key={`${r.entry_id}-${r.side}-${i}`}>
                      <td>{r.entry_id}</td>
                      <td>{r.side}</td>
                      <td>{money(r.mark_mid)}</td>
                      <td>{money(r.realized)}</td>
                      <td>{slipDollars(r.diff)}</td>
                      <td>{money(r.markup)}</td>
                      <td>{slipDollars(r.shortfall)}</td>
                      <td title="NLE-06: no NLE estimate is journaled at entry time in this slice">
                        —
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : (
            <p className="muted">No long recoveries this day.</p>
          )
        ) : (
          <GapNote>
            Only available per-day — open a day's drill-down. /reports/summary has no
            period-aggregated RPT-07 figures in this slice.
          </GapNote>
        )}
      </div>

      <div className="slip-family">
        <h4>RPT-07 — Closes / decay buybacks</h4>
        <GapNote>
          Not yet captured: the event schema doesn't record fill-vs-mark-at-initiation or
          fill-vs-target capture for these two families yet (a documented gap in reports.py).
        </GapNote>
      </div>
    </div>
  );
}
