// RPT-09 daily P&L calendar heatmap. Pins: Monday-first week columns, the
// weekend treatment (distinct from "no trading day"), multi-month rendering
// from a series spanning months, the styled hover box's wins/losses/P&L
// content (honest for a broker-imported day, RPT-16), day-cell click
// navigation to the existing #/results/day/YYYY-MM-DD drill-down, and
// non-trading cells being inert (never clickable).
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DailyRow } from "../../types";
import { CalendarHeatmap } from "./CalendarHeatmap";

// July 2026: the 1st is a Wednesday (Mon-first index 2); the 4th/5th are the
// weekend (Sat/Sun); the 6th is the following Monday (Mon-first index 0).
// August 2026: the 1st is a Saturday (Mon-first index 5); the 3rd is a Monday.
const DAILY: DailyRow[] = [
  { date: "2026-07-01", mode: "paper", net_pnl: "150.00", trust: "bot-computed", wins: 2, losses: 1, entries: 3 },
  { date: "2026-07-06", mode: "paper", net_pnl: "-40.00", trust: "bot-computed", wins: 0, losses: 1, entries: 1 },
  { date: "2026-08-03", mode: "paper", net_pnl: "75.00", trust: "broker-imported", wins: null, losses: null, entries: null },
];

function cellFor(date: string): HTMLElement {
  return screen.getByLabelText(new RegExp(`^${date}:`));
}

describe("CalendarHeatmap (RPT-09)", () => {
  it("renders empty state honestly when there are no trading days at all", () => {
    render(<CalendarHeatmap daily={[]} />);
    expect(screen.getByTestId("heatmap-empty")).toBeInTheDocument();
  });

  it("lays out weeks Monday-first — 2 leading fillers before July 1 (a Wednesday)", () => {
    render(<CalendarHeatmap daily={DAILY} />);
    const july = screen.getByTestId("heatmap-month-2026-07");
    const cells = july.querySelector(".heatmap-grid")!.children;
    expect(cells[0]).toHaveClass("filler");
    expect(cells[1]).toHaveClass("filler");
    // The 3rd grid slot (index 2) is July 1st.
    expect(cells[2]).toHaveAttribute("aria-label", expect.stringContaining("2026-07-01"));
    // July 6th (a Monday) lands back in the Monday column: 2 fillers + 5
    // days (2nd..6th) = index 7.
    expect(cells[7]).toHaveAttribute("aria-label", expect.stringContaining("2026-07-06"));
  });

  it("greys out Saturday/Sunday as a distinct weekend treatment, never conflated with idle", () => {
    const { container } = render(<CalendarHeatmap daily={DAILY} />);
    const saturday = screen.getByTitle("2026-07-04: weekend");
    const sunday = screen.getByTitle("2026-07-05: weekend");
    expect(saturday).toHaveClass("heatmap-cell", "weekend");
    expect(sunday).toHaveClass("heatmap-cell", "weekend");
    // A genuine no-trading weekday (e.g. July 2nd, a Thursday) is the plain
    // "idle" cell instead — a different, honestly-labelled empty state.
    const idleDay = screen.getByTitle("2026-07-02: no trading day");
    expect(idleDay).toHaveClass("idle");
    expect(idleDay).not.toHaveClass("weekend");
    // The legend documents the weekend swatch too.
    expect(container.querySelector(".heatmap-legend")).toHaveTextContent(/weekend/);
  });

  it("renders every month the series spans, side by side, oldest first", () => {
    render(<CalendarHeatmap daily={DAILY} />);
    const strip = screen.getByTestId("heatmap-strip");
    const months = strip.querySelectorAll(".heatmap-month");
    expect(months).toHaveLength(2);
    expect(months[0]).toHaveAttribute("data-testid", "heatmap-month-2026-07");
    expect(months[1]).toHaveAttribute("data-testid", "heatmap-month-2026-08");
    expect(screen.getByText("2026-07")).toBeInTheDocument();
    expect(screen.getByText("2026-08")).toBeInTheDocument();
  });

  it("hovering a trading-day cell shows a styled box with date, entries, wins/losses, and signed P&L (UI-26a)", () => {
    render(<CalendarHeatmap daily={DAILY} />);
    expect(screen.queryByTestId("heatmap-tooltip")).not.toBeInTheDocument();

    fireEvent.mouseEnter(cellFor("2026-07-01"));
    const tip = screen.getByTestId("heatmap-tooltip");
    expect(tip).toHaveTextContent("2026-07-01");
    expect(tip).toHaveTextContent("3 entries · 2 wins · 1 losses");
    expect(tip).toHaveTextContent("+150.00");
    // Signed P&L is colored via the shared gain/loss sign classes.
    expect(tip.querySelector(".heatmap-tooltip-pnl")).toHaveClass("pos");

    fireEvent.mouseLeave(cellFor("2026-07-01"));
    expect(screen.queryByTestId("heatmap-tooltip")).not.toBeInTheDocument();

    fireEvent.mouseEnter(cellFor("2026-07-06"));
    const lossTip = screen.getByTestId("heatmap-tooltip");
    expect(lossTip).toHaveTextContent("1 entries · 0 wins · 1 losses");
    expect(lossTip).toHaveTextContent("-40.00");
    expect(lossTip.querySelector(".heatmap-tooltip-pnl")).toHaveClass("neg");
  });

  it("the hover box appears on keyboard focus too, and never fabricates counts for an imported day", () => {
    render(<CalendarHeatmap daily={DAILY} />);
    fireEvent.focus(cellFor("2026-08-03"));
    const tip = screen.getByTestId("heatmap-tooltip");
    expect(tip).toHaveTextContent("2026-08-03");
    expect(tip).toHaveTextContent("win/loss breakdown not applicable (broker-imported)");
    expect(tip).toHaveTextContent("+75.00");
    expect(tip).not.toHaveTextContent(/0 wins/);
    expect(tip).not.toHaveTextContent(/0 entries/);
    fireEvent.blur(cellFor("2026-08-03"));
    expect(screen.queryByTestId("heatmap-tooltip")).not.toBeInTheDocument();
  });

  it("keeps the full aria-label on trading-day cells (accessibility unchanged)", () => {
    render(<CalendarHeatmap daily={DAILY} />);
    expect(cellFor("2026-07-01")).toHaveAttribute(
      "aria-label",
      "2026-07-01: 3 entries, 2W / 1L, +150.00 (bot-computed)",
    );
    expect(cellFor("2026-08-03")).toHaveAttribute(
      "aria-label",
      "2026-08-03: +75.00 (broker-imported) — win/loss breakdown not applicable (broker-imported)",
    );
  });

  it("a zero-P&L trading day renders as a flat trading cell, visually distinct from a weekend", () => {
    // UI-26a: weekend cells are visually DISTINCT from zero-P&L trading days
    // — a real flat day is data (clickable, "flat" class), a weekend is a
    // decorative non-session cell ("weekend" class, inert).
    const daily: DailyRow[] = [
      ...DAILY,
      { date: "2026-07-07", mode: "paper", net_pnl: "0", trust: "bot-computed", wins: 0, losses: 0, entries: 0 },
    ];
    render(<CalendarHeatmap daily={daily} />);
    const flatDay = cellFor("2026-07-07");
    expect(flatDay).toHaveClass("flat");
    expect(flatDay).not.toHaveClass("weekend");
    expect(flatDay.tagName).toBe("A"); // real data: still click-through to the drill-down
    const weekend = screen.getByTitle("2026-07-04: weekend");
    expect(weekend).toHaveClass("weekend");
    expect(weekend).not.toHaveClass("flat");
  });

  it("a trading-day cell links to the existing day drill-down route", () => {
    render(<CalendarHeatmap daily={DAILY} />);
    const cell = cellFor("2026-07-01");
    expect(cell.tagName).toBe("A");
    expect(cell).toHaveAttribute("href", "#/results/day/2026-07-01");
  });

  it("weekend and no-trading-day cells are inert — never clickable, no hover box", () => {
    render(<CalendarHeatmap daily={DAILY} />);
    const weekend = screen.getByTitle("2026-07-04: weekend");
    const idle = screen.getByTitle("2026-07-02: no trading day");
    expect(weekend.tagName).toBe("SPAN");
    expect(weekend).not.toHaveAttribute("href");
    expect(idle.tagName).toBe("SPAN");
    expect(idle).not.toHaveAttribute("href");
    fireEvent.mouseEnter(weekend);
    fireEvent.mouseEnter(idle);
    expect(screen.queryByTestId("heatmap-tooltip")).not.toBeInTheDocument();
  });
});
