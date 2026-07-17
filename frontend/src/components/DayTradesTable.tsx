import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ET_ZONE, instantToZone } from "../time";
import type { DayTable, DayTableRow, SideBadge, TimingUnmanagedRow } from "../types";
import { dollars, plainDollars } from "./results/shared";
import { Tooltip } from "./Tooltip";

// RPT-17/UI-33 (v1.82): the Trading tab's day-trades table -- TODAY's
// entries, live + closed, one row each -- plus the Timing & Unmanaged
// report below it. Read-only: every figure is server-computed off the ONE
// canonical aggregation path (RPT-09a) via GET /reports/day-table; this
// component recomputes nothing, it only formats and labels.

// UI-26 shape+colour together, never colour alone -- reuses the SAME
// `.ec-badge.s-*` classes/icons EntryCards.tsx's STATUS_META already
// defines, just keyed by the per-SIDE badge value instead of the whole
// entry's status.
const SIDE_BADGE_META: Record<SideBadge, { label: string; cls: string; icon: string }> = {
  open: { label: "Open", cls: "s-pending", icon: "○" },
  protected: { label: "Protected", cls: "s-protected", icon: "🛡️" },
  stopped: { label: "Stopped", cls: "s-stopped", icon: "🔴" },
  decay: { label: "Decay", cls: "s-decay", icon: "📉" },
  closed: { label: "Closed", cls: "s-closed", icon: "📕" },
  expired: { label: "Expired", cls: "s-expired", icon: "⌛" },
};

function etTime(iso: string | null): string {
  if (!iso) return "—";
  return instantToZone(iso, ET_ZONE) ?? "—";
}

function SideCell({ side, row }: { side: "PUT" | "CALL"; row: DayTableRow }) {
  const badge = SIDE_BADGE_META[row.side_badges[side]];
  const strikes = row.strikes[side];
  const width = row.wing_width[side];
  return (
    <td className="dt-side-cell">
      <span className={`ec-badge ${badge.cls}`} title={`${side} side: ${badge.label}`}>
        {badge.icon} {badge.label}
      </span>
      <div className="dt-strikes">
        {strikes.short ?? "—"}/{strikes.long ?? "—"}
        {width != null && <span className="muted"> (w {width})</span>}
      </div>
    </td>
  );
}

function PnlCell({ row }: { row: DayTableRow }) {
  if (row.pnl == null) {
    return <td className="dt-pnl">—</td>;
  }
  const n = Number(row.pnl);
  return (
    <td className="dt-pnl">
      <span className={n >= 0 ? "pos" : "neg"} title={row.pnl}>
        {dollars(row.pnl)}
      </span>
      {row.pnl_unrealized && <span className="tag" data-testid="unrealized-tag">unrealized</span>}
      {row.provisional && (
        <span className="tag pending" title="Broker settlement not yet captured/reconciled (EOD-01)"
              data-testid="provisional-tag">
          PROVISIONAL
        </span>
      )}
    </td>
  );
}

function DayTotalRow({ total }: { total: DayTable["day_total"] }) {
  if (!total) return null;
  return (
    <tr className="dt-total-row" data-testid="day-total-row">
      <td colSpan={4}><strong>Day total</strong></td>
      <td title={total.total_credit}>{plainDollars(total.total_credit)}</td>
      <td colSpan={2} />
      <td />
      <td>{total.stop_fill_count}</td>
      <td className={Number(total.net_pnl) >= 0 ? "pos" : "neg"} title={total.net_pnl}>
        <strong>{dollars(total.net_pnl)}</strong>
      </td>
      <td />
    </tr>
  );
}

function TimingUnmanagedTable({ rows }: { rows: TimingUnmanagedRow[] }) {
  return (
    <section className="card" data-testid="timing-unmanaged">
      <div className="card-head">
        <h2>Timing &amp; Unmanaged</h2>
        <Tooltip
          id="unmanaged-caption"
          testId="unmanaged-caption-tooltip"
          label="What is Unmanaged P&L?"
          content={"Unmanaged P&L = premium received minus the entry's spread value at the "
            + "16:00 ET close, computed only from recorded quote samples (D8b). It shows what "
            + "doing nothing at all would have made -- display and analytics ONLY, never a "
            + "trading input and never a correction to the realized P&L above."}
        />
      </div>
      {rows.length === 0 ? (
        <p className="muted">No entries today.</p>
      ) : (
        <table className="entries" data-testid="timing-unmanaged-table">
          <thead>
            <tr>
              <th>Entry</th>
              <th>Opened (ET)</th>
              <th>Closed (ET)</th>
              <th>Premium</th>
              <th>Realized P&amp;L</th>
              <th>Unmanaged P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.entry_id}>
                <td>{r.entry_id}</td>
                <td>{etTime(r.opened_at)}</td>
                <td>{etTime(r.closed_at)}</td>
                <td title={r.premium}>{plainDollars(r.premium)}</td>
                <td className={r.realized_pnl == null ? "" : Number(r.realized_pnl) >= 0 ? "pos" : "neg"}>
                  {r.realized_pnl == null ? "—" : dollars(r.realized_pnl)}
                </td>
                <td data-testid="unmanaged-cell">
                  {r.unmanaged_status === "no_data" ? (
                    <span className="muted">no data (not sampled)</span>
                  ) : (
                    <span className={Number(r.unmanaged_pnl) >= 0 ? "pos" : "neg"} title={r.unmanaged_pnl ?? ""}>
                      {dollars(r.unmanaged_pnl)}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function DayTradesTable() {
  const [data, setData] = useState<DayTable | null>(null);
  const poll = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => {
      api.getDayTable().then((d) => { if (alive) setData(d); }).catch(() => { /* keep last-known */ });
    };
    load();
    // Live rows (still-open entries) update in place -- poll like the rest
    // of the Trading tab's live surfaces (useLiveBot's own REST fallback
    // cadence is 1.5s; this table's figures are cheaper to recompute and
    // less latency-critical, so 4s keeps it visibly live without hammering
    // the endpoint every render).
    poll.current = window.setInterval(load, 4000);
    return () => {
      alive = false;
      if (poll.current) window.clearInterval(poll.current);
    };
  }, []);

  if (!data || data.rows.length === 0) {
    return (
      <section className="card" data-testid="day-trades-table">
        <div className="card-head"><h2>Today's trades</h2></div>
        <p className="muted">No entries yet today.</p>
      </section>
    );
  }

  return (
    <>
      <section className="card" data-testid="day-trades-table">
        <div className="card-head">
          <h2>Today's trades</h2>
          <span className="muted">{data.rows.length} entr{data.rows.length === 1 ? "y" : "ies"}</span>
        </div>
        <div className="dt-scroll">
          <table className="entries" data-testid="day-trades-rows">
            <thead>
              <tr>
                <th>Entry</th>
                <th>Time (ET)</th>
                <th>Source</th>
                <th>Target premium</th>
                <th>Net credit</th>
                <th>PUT</th>
                <th>CALL</th>
                <th>SPX ref</th>
                <th>Stops</th>
                <th>P&amp;L</th>
                <th>Closed (ET)</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row) => (
                <tr key={row.entry_id} data-testid={`day-trade-row-${row.entry_id}`}>
                  <td>{row.entry_id}</td>
                  <td>{etTime(row.entry_time)}</td>
                  <td>{row.initiator ?? "—"}</td>
                  <td title={row.target_premium ?? ""}>
                    {row.target_premium == null ? "—" : `$${row.target_premium}`}
                  </td>
                  <td title={row.net_credit}>{plainDollars(row.net_credit)}</td>
                  <SideCell side="PUT" row={row} />
                  <SideCell side="CALL" row={row} />
                  <td title={row.spx_reference.value ?? ""}>
                    {row.spx_reference.value == null ? "—"
                      : `${row.spx_reference.value} (${row.spx_reference.label})`}
                  </td>
                  <td>{row.stop_fill_count}</td>
                  <PnlCell row={row} />
                  <td>{etTime(row.closed_at)}</td>
                </tr>
              ))}
              <DayTotalRow total={data.day_total} />
            </tbody>
          </table>
        </div>
      </section>
      <TimingUnmanagedTable rows={data.timing_unmanaged} />
    </>
  );
}
