// RPT-17/UI-33 -- the Trading tab's day-trades table + Timing & Unmanaged
// report. Pins what it RENDERS from a mocked GET /reports/day-table: per-side
// badges, credits, realized P&L net of fees, the live/unrealized badge on an
// open row updating in place, and the honest "no data (not sampled)" /
// PROVISIONAL states -- never a client-side recompute (UI-03/RPT-09a).
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import type { DayTable, DayTableRow } from "../types";
import { DayTradesTable } from "./DayTradesTable";

function row(overrides: Partial<DayTableRow> = {}): DayTableRow {
  return {
    entry_id: "2026-07-09#1",
    status: "EXPIRED",
    entry_time: "2026-07-09T13:32:00+00:00",
    closed_at: "2026-07-09T20:00:00+00:00",
    initiator: "schedule",
    target_premium: "3.50",
    net_credit: "360.00",
    wing_width: { PUT: "25", CALL: "25" },
    strikes: { PUT: { short: "7535", long: "7510" }, CALL: { short: "7540", long: "7565" } },
    spx_reference: { value: "7541", label: "close" },
    side_badges: { PUT: "expired", CALL: "expired" },
    stop_fill_count: 0,
    pnl: "355.12",
    pnl_unrealized: false,
    provisional: false,
    ...overrides,
  };
}

function table(rows: DayTableRow[], overrides: Partial<DayTable> = {}): DayTable {
  return {
    date: "2026-07-09",
    mode: "paper",
    rows,
    day_total: {
      net_pnl: "355.12", fees: "4.88", total_credit: "360.00", filled: rows.length, stop_fill_count: 0,
    },
    timing_unmanaged: rows.map((r) => ({
      entry_id: r.entry_id, opened_at: r.entry_time, closed_at: r.closed_at,
      premium: r.net_credit, realized_pnl: r.pnl, unmanaged_pnl: null, unmanaged_status: "no_data",
    })),
    ...overrides,
  };
}

describe("DayTradesTable", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("shows per-side badges, net credit, and realized P&L net of fees for a closed entry", async () => {
    vi.spyOn(api, "getDayTable").mockResolvedValue(table([row()]));
    render(<DayTradesTable />);

    await screen.findByTestId("day-trade-row-2026-07-09#1");
    expect(screen.getByTestId("day-trades-rows")).toHaveTextContent("$360");
    expect(screen.getByTestId("day-trades-rows")).toHaveTextContent("+$355.12");
    const badges = screen.getAllByText(/Expired/);
    expect(badges.length).toBeGreaterThanOrEqual(2); // one per side (PUT + CALL)
  });

  it("badges an open row's live P&L as unrealized and updates it in place on the next poll", async () => {
    vi.spyOn(api, "getDayTable")
      .mockResolvedValueOnce(table([row({
        status: "PROTECTED", closed_at: null, pnl: "12.00", pnl_unrealized: true,
        side_badges: { PUT: "protected", CALL: "protected" },
      })]))
      .mockResolvedValueOnce(table([row({
        status: "PROTECTED", closed_at: null, pnl: "48.00", pnl_unrealized: true,
        side_badges: { PUT: "protected", CALL: "protected" },
      })]));

    render(<DayTradesTable />);

    await waitFor(() => expect(screen.getByTestId("day-trades-rows")).toHaveTextContent("+$12.00"));
    expect(screen.getByTestId("unrealized-tag")).toBeInTheDocument();

    await vi.advanceTimersByTimeAsync(4000);

    await waitFor(() => expect(screen.getByTestId("day-trades-rows")).toHaveTextContent("+$48.00"));
  });

  it('renders "no data (not sampled)" for a missing close-time sample, never an interpolation', async () => {
    vi.spyOn(api, "getDayTable").mockResolvedValue(table([row()]));
    render(<DayTradesTable />);

    await screen.findByTestId("timing-unmanaged-table");
    expect(screen.getByTestId("unmanaged-cell")).toHaveTextContent("no data (not sampled)");
  });

  it("shows the recorded Unmanaged P&L figure when the close-time sample exists", async () => {
    const t = table([row()]);
    t.timing_unmanaged = [{
      entry_id: "2026-07-09#1", opened_at: row().entry_time, closed_at: row().closed_at,
      premium: "360.00", realized_pnl: "355.12", unmanaged_pnl: "278.00", unmanaged_status: "ok",
    }];
    vi.spyOn(api, "getDayTable").mockResolvedValue(t);
    render(<DayTradesTable />);

    await screen.findByTestId("timing-unmanaged-table");
    expect(screen.getByTestId("unmanaged-cell")).toHaveTextContent("+$278.00");
  });

  it("renders the EOD-01 PROVISIONAL label on a settlement-pending row, never fake finality", async () => {
    vi.spyOn(api, "getDayTable").mockResolvedValue(table([row({ provisional: true })]));
    render(<DayTradesTable />);

    expect(await screen.findByTestId("provisional-tag")).toHaveTextContent("PROVISIONAL");
  });

  it("renders a day-total row", async () => {
    vi.spyOn(api, "getDayTable").mockResolvedValue(table([row()]));
    render(<DayTradesTable />);

    await screen.findByTestId("day-total-row");
    expect(screen.getByTestId("day-total-row")).toHaveTextContent("Day total");
    expect(screen.getByTestId("day-total-row")).toHaveTextContent("+$355.12");
  });

  it("shows an honest empty state before any entries fire today", async () => {
    vi.spyOn(api, "getDayTable").mockResolvedValue(table([], { day_total: null, timing_unmanaged: [] }));
    render(<DayTradesTable />);

    expect(await screen.findByText(/No entries yet today/i)).toBeInTheDocument();
  });
});
