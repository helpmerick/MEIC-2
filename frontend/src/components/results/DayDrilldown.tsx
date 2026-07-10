import { useEffect, useState } from "react";
import { api } from "../../api";
import type { DayReportDetail } from "../../types";
import { CsvButton, GapNote, PaperBanner, TrustBadge } from "./shared";
import { SlippagePanels } from "./SlippagePanels";
import { Timeline } from "./Timeline";

// UI-27 deep link target: #/results/day/YYYY-MM-DD. Entries, legs, fills,
// stops, skip reasons, the RPT-12 timeline, and RPT-15 corrections side by
// side (bot-computed vs broker truth), all from one GET /reports/day/{date}.
export function DayDrilldown({ date }: { date: string }) {
  const [detail, setDetail] = useState<DayReportDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError(null);
    api
      .getReportDay(date)
      .then((d) => {
        if (alive) setDetail(d);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [date]);

  return (
    <div className="results-page" data-testid="day-drilldown">
      <a className="back-link" href="#/results">
        ← Back to Results
      </a>
      <h1 className="results-title">Day drill-down — {date}</h1>

      {error && <div className="banner-error">Could not load this day — {error}</div>}
      {!detail && !error && <p className="muted">Loading…</p>}

      {detail && (
        <>
          <PaperBanner mode={detail.mode} />

          <section className="card">
            <div className="card-head">
              <h2>{date}</h2>
              <TrustBadge trust={detail.trust} />
            </div>
            <div className="row">
              <CsvButton table="entries" period={{ day: date }} label="Export entries CSV" />
              <CsvButton table="corrections" period={{ day: date }} label="Export corrections CSV" />
            </div>
          </section>

          {detail.imported_fills && detail.imported_fills.length > 0 && (
            <section className="card">
              <h2>RPT-16 — Broker-imported fills</h2>
              <p className="muted">
                This day predates the event journal and was imported from broker history
                (cash-level only — no recorded trading intent).
              </p>
              {detail.imported_cash && (
                <p>
                  Net cash: <strong>${detail.imported_cash.net}</strong> · Fees: $
                  {detail.imported_cash.fees}
                </p>
              )}
              <table className="entries" data-testid="imported-fills-table">
                <thead>
                  <tr>
                    <th>Order</th>
                    <th>Symbol</th>
                    <th>Action</th>
                    <th>Qty</th>
                    <th>Price</th>
                    <th>Fee</th>
                    {/* RPT-16 settlement import (operator ruling 2026-07-10): a
                        Receive-Deliver row's signed net cash effect, real dollars. */}
                    <th>Value</th>
                    <th>At</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.imported_fills.map((f, i) => (
                    <tr key={i} className={f.value != null ? "imported-settlement-row" : undefined}>
                      <td>{f.order_id}</td>
                      <td>{f.symbol}</td>
                      <td>{f.action}</td>
                      <td>{f.quantity}</td>
                      <td>{f.price ?? "—"}</td>
                      <td>{f.fee ?? "—"}</td>
                      <td className={f.value != null ? (Number(f.value) >= 0 ? "pos" : "neg") : undefined}>
                        {f.value ?? "—"}
                      </td>
                      <td>{f.at}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          <section className="card">
            <h2>RPT-12 — Intraday timeline</h2>
            <Timeline timeline={detail.timeline} />
          </section>

          <section className="card">
            <h2>Entries</h2>
            {detail.entries.length === 0 ? (
              <p className="muted">No entries this day.</p>
            ) : (
              <>
                <table className="entries" data-testid="day-entries-table">
                  <thead>
                    <tr>
                      <th>Entry</th>
                      <th>Status</th>
                      <th>Outcome</th>
                      <th>Credit (per-share)</th>
                      <th>P&amp;L (per-share)</th>
                      <th>Fees (per-share)</th>
                      <th>Stopped</th>
                      <th>Expired</th>
                      <th>Closed by</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.entries.map((e) => (
                      <tr key={e.entry_id}>
                        <td>{e.entry_id}</td>
                        <td>
                          {e.status}
                          {e.settlement_pending && (
                            <span className="tag pending" title="Broker settlement not yet captured (EOD-01)">
                              {" "}
                              provisional
                            </span>
                          )}
                        </td>
                        <td>{e.outcome ?? "—"}</td>
                        <td>{e.net_credit}</td>
                        <td className={Number(e.pnl) >= 0 ? "pos" : "neg"}>{e.pnl}</td>
                        <td>{e.fees}</td>
                        <td>{e.sides_stopped.join("+") || "—"}</td>
                        <td>{e.sides_expired.join("+") || "—"}</td>
                        <td>{e.close_initiator ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <GapNote>
                  Per-entry credit/P&amp;L/fees above are PER-SHARE values, not real dollars (a
                  documented API gap — reports.py's report_day handler uses the raw projection,
                  unlike the dollarized period aggregates elsewhere on this dashboard). A real
                  dollar figure needs × 100 × contracts, not done here to avoid re-deriving
                  trading arithmetic client-side (UI-03).
                </GapNote>
              </>
            )}
          </section>

          {detail.skips.length > 0 && (
            <section className="card">
              <h2>Skips</h2>
              <ul>
                {detail.skips.map((s) => (
                  <li key={s.entry_number}>
                    entry {s.entry_number}: <code>{s.reason}</code>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className="card">
            <h2>Slippage</h2>
            <SlippagePanels daySlippage={detail.slippage} />
          </section>

          {detail.corrections.length > 0 && (
            <section className="card">
              <h2>RPT-15 corrections — bot-computed vs broker truth</h2>
              <table className="entries" data-testid="corrections-table">
                <thead>
                  <tr>
                    <th>Field</th>
                    <th>Bot-computed</th>
                    <th>Broker truth</th>
                    <th>Diff</th>
                    <th>At</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.corrections.map((c, i) => (
                    <tr key={i}>
                      <td>{c.field}</td>
                      <td>{c.bot_value}</td>
                      <td>{c.broker_value}</td>
                      <td>{c.diff}</td>
                      <td>{c.at}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </>
      )}
    </div>
  );
}
