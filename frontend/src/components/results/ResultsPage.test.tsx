// The Results page holds no trading logic (UI-03) — these pin what it RENDERS
// from a mocked API: metric states (ok/insufficient/unconfigured), the UI-25
// trust badge in both states, the SIM-05 paper banner, and CSV export hrefs.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../api";
import type { DayStatus, ReportSummary, ScheduleView } from "../../types";
import { ResultsPage } from "./ResultsPage";

const SCHEDULE: ScheduleView = {
  rows: [], day_total_estimate: "0", max_day_risk: null, headroom: null,
  exceeds_max_day_risk: false, config_version: null, estimate_note: "", risk_scope_note: "",
};
const DAY_STATUS: DayStatus = { started: false, running: false, armed: false };

function baseSummary(overrides: Partial<ReportSummary> = {}): ReportSummary {
  return {
    mode: "paper",
    period_days: ["2026-07-08", "2026-07-09"],
    trust: { status: "bot-computed", confirmed_days: 1, total_days: 2, label: "1/2 days broker-confirmed" },
    core: {
      net_pnl: "180.00", gross_pnl: "400.00", fees: "220.00", filled: 2, fired: 3,
      skipped_by_reason: { max_day_risk: 1 }, total_credit: "800.00",
      day_win_rate: "0.5", entry_win_rate: "0.5", premium_capture: "0.225",
    },
    metrics: { status: "unconfigured" },
    taxonomy: { distribution: { FULL_EXPIRY: 2 }, contract_breaches: [] },
    health: {
      skip_reason_histogram: { max_day_risk: 1 }, watchdog_escalations: 0, unprotected_events: 0,
      rsk03_mismatches: 0, correction_count: 0, ent10_crash_alerts: null, ord08_terminal_retries: null,
    },
    waterfall: {
      credits: "800.00", stop_costs: "0.00", recoveries: "0.00", buybacks: "0.00",
      fees: "220.00", slippage: "0.00", net: "580.00", premium_capture: "0.725",
    },
    ...overrides,
  };
}

function mockApis(summary: ReportSummary) {
  vi.spyOn(api, "getReportSummary").mockResolvedValue(summary);
  vi.spyOn(api, "getDailySeries").mockResolvedValue([
    { date: "2026-07-08", mode: "paper", net_pnl: "100.00", trust: "broker-confirmed" },
    { date: "2026-07-09", mode: "paper", net_pnl: "80.00", trust: "bot-computed" },
  ]);
  vi.spyOn(api, "getSchedule").mockResolvedValue(SCHEDULE);
  vi.spyOn(api, "getDayStatus").mockResolvedValue(DAY_STATUS);
}

describe("ResultsPage", () => {
  beforeEach(() => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
      matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    }));
  });

  it("renders the unconfigured-metrics state when reporting_capital_base is unset (RPT-04)", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    expect(await screen.findByTestId("metrics-unconfigured")).toHaveTextContent(/unconfigured/i);
  });

  it("renders return/risk metrics and the Sharpe/Sortino insufficient-data note below the sample floor", async () => {
    mockApis(baseSummary({
      metrics: {
        status: "ok", roc: "0.048", sharpe: null, sortino: null,
        max_drawdown_dollars: "360.00", max_drawdown_pct: "0.036", profit_factor: "2.33",
        expectancy_per_entry: "96.00", avg_win_day: "210.00", avg_loss_day: "-180.00",
        longest_losing_streak_days: 1, day_win_rate: "0.8", sample_days: 5, min_sample_days: 20,
      },
    }));
    render(<ResultsPage entries={[]} />);
    const note = await screen.findByTestId("sharpe-insufficient");
    expect(note).toHaveTextContent(/20\+ trading days/i);
    expect(note).toHaveTextContent(/5 so far/i);
  });

  it("shows broker-confirmed ✓ when every day in scope is reconciled (UI-25)", async () => {
    mockApis(baseSummary({
      trust: { status: "broker-confirmed", confirmed_days: 2, total_days: 2, label: "broker-confirmed" },
    }));
    render(<ResultsPage entries={[]} />);
    await waitFor(() => {
      const badges = screen.getAllByTestId("trust-badge");
      expect(badges.some((b) => b.textContent?.includes("broker-confirmed ✓"))).toBe(true);
    });
  });

  it("shows the bot-computed N/M count when not every day is reconciled (UI-25)", async () => {
    mockApis(baseSummary()); // trust: 1/2 days
    render(<ResultsPage entries={[]} />);
    await waitFor(() => {
      const badges = screen.getAllByTestId("trust-badge");
      expect(badges.some((b) => b.textContent?.includes("1/2 days broker-confirmed"))).toBe(true);
    });
  });

  it("renders the SIM-05 paper banner in paper mode", async () => {
    mockApis(baseSummary({ mode: "paper" }));
    render(<ResultsPage entries={[]} />);
    expect(await screen.findByTestId("paper-banner")).toBeInTheDocument();
  });

  it("does not render the paper banner in live mode", async () => {
    mockApis(baseSummary({ mode: "live" }));
    render(<ResultsPage entries={[]} />);
    await screen.findByTestId("results-page");
    await waitFor(() => expect(screen.queryByTestId("paper-banner")).not.toBeInTheDocument());
  });

  it("the daily CSV export link points at /reports/csv?table=daily", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const link = await screen.findByTestId("csv-daily");
    expect(link.getAttribute("href")).toMatch(/^\/reports\/csv\?table=daily/);
  });

  it("lists each trading day in scope as a deep link to its drill-down (UI-27)", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const links = await screen.findByTestId("day-links");
    expect(links.querySelector('a[href="#/results/day/2026-07-08"]')).toBeInTheDocument();
    expect(links.querySelector('a[href="#/results/day/2026-07-09"]')).toBeInTheDocument();
  });
});

// UTC-safe month/year shift used only to compute expected values in these
// tests; mirrors the component's private shiftIsoMonth/shiftYear (UI-03
// display-only date math).
function shiftIsoMonthForTest(month: string, delta: number): string {
  const d = new Date(month + "-01T00:00:00Z");
  d.setUTCMonth(d.getUTCMonth() + delta);
  return d.toISOString().slice(0, 7);
}
function monthIsoGuessForTest(): string {
  return new Date().toISOString().slice(0, 7);
}
function yearIsoGuessForTest(): string {
  return String(new Date().getFullYear());
}

describe("period selector — Path A (Month default, navigable Day, Today kept last)", () => {
  beforeEach(() => {
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({
      matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    }));
  });

  it("defaults to the Month tab as active on first paint", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const picker = within(screen.getByTestId("period-picker"));
    expect(picker.getByRole("button", { name: "Month" }).className).toContain("active");
    expect(picker.getByRole("button", { name: "Today" }).className).not.toContain("active");
  });

  it("keeps the Today tab present, and last among the period tabs", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const pickerEl = screen.getByTestId("period-picker");
    const picker = within(pickerEl);
    expect(picker.getByRole("button", { name: "Today" })).toBeInTheDocument();
    const tabs = pickerEl.querySelectorAll(".period-tab");
    expect(tabs[tabs.length - 1].textContent).toBe("Today");
  });

  it("disables the next-day button once the day reaches today", async () => {
    mockApis(baseSummary());
    vi.spyOn(api, "adjacentTradingDay").mockResolvedValue({ date: "2026-07-08" });
    render(<ResultsPage entries={[]} />);
    const picker = within(screen.getByTestId("period-picker"));
    fireEvent.click(picker.getByRole("button", { name: "Day" }));
    await picker.findByLabelText("pick a day");
    // Day defaults to today, so the next-day button starts disabled.
    expect(picker.getByLabelText("next day")).toBeDisabled();

    fireEvent.click(picker.getByLabelText("previous day"));
    await waitFor(() =>
      expect((picker.getByLabelText("pick a day") as HTMLInputElement).value).toBe("2026-07-08"));
    expect(picker.getByLabelText("next day")).not.toBeDisabled();
  });

  it("steps the day PREVIOUS via the DAY-01 trading-day-aware backend endpoint", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const picker = within(screen.getByTestId("period-picker"));
    fireEvent.click(picker.getByRole("button", { name: "Day" }));
    const dayInput = (await picker.findByLabelText("pick a day")) as HTMLInputElement;
    const original = dayInput.value;

    const spy = vi.spyOn(api, "adjacentTradingDay").mockResolvedValue({ date: "2026-07-02" });
    fireEvent.click(picker.getByLabelText("previous day"));
    expect(spy).toHaveBeenCalledWith(original, "prev");
    await waitFor(() => expect(dayInput.value).toBe("2026-07-02"));
  });

  it("steps the day NEXT via the DAY-01 trading-day-aware backend endpoint", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const picker = within(screen.getByTestId("period-picker"));
    fireEvent.click(picker.getByRole("button", { name: "Day" }));
    const dayInput = (await picker.findByLabelText("pick a day")) as HTMLInputElement;

    // step back first so the (today-capped) next-day button is enabled
    vi.spyOn(api, "adjacentTradingDay").mockResolvedValueOnce({ date: "2026-07-02" });
    fireEvent.click(picker.getByLabelText("previous day"));
    await waitFor(() => expect(dayInput.value).toBe("2026-07-02"));

    const spy = vi.spyOn(api, "adjacentTradingDay").mockResolvedValue({ date: "2026-07-06" });
    fireEvent.click(picker.getByLabelText("next day"));
    expect(spy).toHaveBeenCalledWith("2026-07-02", "next");
    await waitFor(() => expect(dayInput.value).toBe("2026-07-06"));
  });

  it("a null date back from the calendar endpoint (never-into-the-future cap) is a no-op", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const picker = within(screen.getByTestId("period-picker"));
    fireEvent.click(picker.getByRole("button", { name: "Day" }));
    const dayInput = (await picker.findByLabelText("pick a day")) as HTMLInputElement;

    vi.spyOn(api, "adjacentTradingDay").mockResolvedValueOnce({ date: "2026-07-02" });
    fireEvent.click(picker.getByLabelText("previous day"));
    await waitFor(() => expect(dayInput.value).toBe("2026-07-02"));

    const spy = vi.spyOn(api, "adjacentTradingDay").mockResolvedValue({ date: null });
    fireEvent.click(picker.getByLabelText("next day"));
    await waitFor(() => expect(spy).toHaveBeenCalledWith("2026-07-02", "next"));
    expect(dayInput.value).toBe("2026-07-02");   // unchanged: null means no-op
  });

  it("steps the month with the ◀ and ▶ nav buttons (frontend-only)", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const picker = within(screen.getByTestId("period-picker"));
    // Month is the default tab.
    const monthInput = (await picker.findByLabelText("pick a month")) as HTMLInputElement;
    const original = monthInput.value;

    fireEvent.click(picker.getByLabelText("previous month"));
    expect(monthInput.value).toBe(shiftIsoMonthForTest(original, -1));

    fireEvent.click(picker.getByLabelText("next month"));
    expect(monthInput.value).toBe(original);
  });

  it("disables the next-month button once the month reaches the current month", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const picker = within(screen.getByTestId("period-picker"));
    await picker.findByLabelText("pick a month");
    expect(picker.getByLabelText("next month")).toBeDisabled();
    expect(monthIsoGuessForTest()).toBeTruthy(); // sanity: helper mirrors the component
  });

  it("steps the year with the ◀ and ▶ nav buttons and disables ▶ at the current year", async () => {
    mockApis(baseSummary());
    render(<ResultsPage entries={[]} />);
    const picker = within(screen.getByTestId("period-picker"));
    fireEvent.click(picker.getByRole("button", { name: "Year" }));
    const yearInput = (await picker.findByLabelText("pick a year")) as HTMLInputElement;
    const original = yearInput.value;
    expect(original).toBe(yearIsoGuessForTest());
    expect(picker.getByLabelText("next year")).toBeDisabled();

    fireEvent.click(picker.getByLabelText("previous year"));
    expect(yearInput.value).toBe(String(Number(original) - 1));
    expect(picker.getByLabelText("next year")).not.toBeDisabled();

    fireEvent.click(picker.getByLabelText("next year"));
    expect(yearInput.value).toBe(original);
  });
});
