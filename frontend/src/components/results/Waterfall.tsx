import type { WaterfallResult } from "../../types";
import { dollars, pct, plainDollars } from "./shared";

// RPT-11 P&L attribution waterfall. A reconciliation residual is an explicit
// error state (never a silently adjusted bar) — the backend already refuses
// to build the waterfall when the bars don't add up to the cent.
export function Waterfall({ wf }: { wf: WaterfallResult }) {
  if ("error" in wf) {
    return (
      <div className="waterfall-error" data-testid="waterfall-error" role="alert">
        <strong>RPT-11 reconciliation FAILED — residual {dollars(wf.residual)}</strong>
        <p>
          Expected net {plainDollars(wf.expected_net)}, computed {plainDollars(wf.computed_net)}.
          Never silently adjusted — this is a bug to investigate, not a rendering choice.
        </p>
      </div>
    );
  }

  const credits = Number(wf.credits) || 0;
  const rows: { label: string; value: string; kind: "add" | "sub" }[] = [
    { label: "Credits collected", value: wf.credits, kind: "add" },
    { label: "Stop-out costs", value: wf.stop_costs, kind: "sub" },
    { label: "Long recoveries", value: wf.recoveries, kind: "add" },
    { label: "Close/decay buybacks", value: wf.buybacks, kind: "sub" },
    { label: "Fees", value: wf.fees, kind: "sub" },
    { label: "Net slippage", value: wf.slippage, kind: "sub" },
  ];

  return (
    <div className="waterfall" data-testid="waterfall">
      {rows.map((r) => {
        const v = Math.abs(Number(r.value) || 0);
        const width = credits > 0 ? Math.min(100, (v / credits) * 100) : 0;
        return (
          <div key={r.label} className="wf-row">
            <span className="wf-label">{r.label}</span>
            <div className="wf-bar-track">
              <div className={`wf-bar ${r.kind}`} style={{ width: `${width}%` }} />
            </div>
            <span className="wf-value" title={r.value}>
              {r.kind === "sub" ? "−" : "+"}
              {plainDollars(r.value)}
              {credits > 0 && ` (${width.toFixed(1)}%)`}
            </span>
          </div>
        );
      })}
      <div className="wf-row wf-net">
        <span className="wf-label">= Net P&amp;L</span>
        <div className="wf-bar-track">
          <div
            className={`wf-bar ${Number(wf.net) >= 0 ? "add" : "sub"}`}
            style={{
              width: `${credits > 0 ? Math.min(100, (Math.abs(Number(wf.net)) / credits) * 100) : 0}%`,
            }}
          />
        </div>
        <span className="wf-value" title={wf.net}>
          {dollars(wf.net)}
          {wf.premium_capture != null && ` (${pct(wf.premium_capture)} of collected)`}
        </span>
      </div>
    </div>
  );
}
