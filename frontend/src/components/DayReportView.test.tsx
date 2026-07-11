import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { DayReportView } from "./DayReportView";
import type { DayReport, EntryCard } from "../types";

function report(over: Partial<DayReport> = {}): DayReport {
  return {
    date: "2026-07-11", entries_filled: 1, stops_hit: 0, lex_recoveries: 0, decay_closes: 0,
    total_credit: "5.20", total_fees: "0.02", day_pnl: "0.40",
    skips: [], per_entry_pnl: { "2026-07-11#1": "0.40" },
    ...over,
  };
}

function entry(over: Partial<EntryCard> = {}): EntryCard {
  return {
    entry_id: "2026-07-11#1", status: "PROTECTED", net_credit: "5.20", pnl: "0.40",
    sides_stopped: [], sides_expired: [], recovered: false, close_initiator: null,
    ...over,
  };
}

// Operator request 2026-07-11: the Credit / Day P&L tiles and the per-entry
// table show real contract dollars (premium x100 x contracts), not the raw
// per-share Decimal `report` carries.
describe("DayReportView — contract-dollar totals (operator request 2026-07-11)", () => {
  it("shows placeholder state with no report", () => {
    render(<DayReportView report={null} entries={[]} />);
    expect(screen.getByText("Day report")).toBeInTheDocument();
  });

  it("converts the Credit and Day P&L tiles to real dollars for a 1-contract entry", () => {
    render(<DayReportView report={report()} entries={[entry()]} />);
    expect(screen.getByText("+$520")).toBeInTheDocument(); // Credit (unique — no per-entry credit column)
    // Day P&L tile AND the matching per-entry row both show "+$40" here.
    expect(screen.getAllByText("+$40").length).toBe(2);
  });

  it("converts a per-entry P&L row to real dollars", () => {
    render(<DayReportView report={report()} entries={[entry()]} />);
    // Entry column ("2026-07-11#1") + the P&L cell ("+$40")
    expect(screen.getByText("2026-07-11#1")).toBeInTheDocument();
    expect(screen.getAllByText("+$40").length).toBeGreaterThan(0);
  });

  it("sums correctly across entries with DIFFERENT contracts counts (ENT-04) rather than applying one flat multiplier", () => {
    const legs1x = [
      { side: "PUT" as const, role: "short" as const, strike: "7535", price: "1.80", qty: 1 },
    ];
    const legs3x = [
      { side: "PUT" as const, role: "short" as const, strike: "7535", price: "1.80", qty: 3 },
    ];
    const r = report({
      total_credit: "9.20", day_pnl: "1.60",
      per_entry_pnl: { "2026-07-11#1": "0.40", "2026-07-11#2": "0.40" },
    });
    const entries = [
      entry({ entry_id: "2026-07-11#1", net_credit: "5.20", pnl: "0.40", legs: legs1x }),
      entry({ entry_id: "2026-07-11#2", net_credit: "4.00", pnl: "0.40", legs: legs3x }),
    ];
    render(<DayReportView report={r} entries={entries} />);
    // credit: (5.20*100*1) + (4.00*100*3) = 520 + 1200 = 1720
    expect(screen.getByText("+$1720")).toBeInTheDocument();
    // day P&L: (0.40*100*1) + (0.40*100*3) = 40 + 120 = 160
    expect(screen.getByText("+$160")).toBeInTheDocument();
    // per-entry rows scale individually
    expect(screen.getByText("+$40")).toBeInTheDocument();
    expect(screen.getByText("+$120")).toBeInTheDocument();
  });

  it("shows a negative Day P&L tile in red-class real dollars", () => {
    const r = report({ day_pnl: "-1.05", per_entry_pnl: { "2026-07-11#1": "-1.05" } });
    render(<DayReportView report={r} entries={[entry({ net_credit: "5.20", pnl: "-1.05" })]} />);
    // Both the Day P&L tile and the matching per-entry row show "-$105".
    const els = screen.getAllByText("-$105");
    expect(els.length).toBe(2);
    for (const el of els) expect(el).toHaveClass("neg");
  });
});
