// The Results page holds no trading logic (UI-03) — these pin what it RENDERS
// from a mocked API: metric states (ok/insufficient/unconfigured), the UI-25
// trust badge in both states, the SIM-05 paper banner, and CSV export hrefs.
import { render, screen, waitFor } from "@testing-library/react";
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
