import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TrustBlock } from "../../types";
import { CsvButton, PaperBanner, TrustBadge } from "./shared";

describe("TrustBadge (UI-25)", () => {
  it("shows broker-confirmed ✓ when the whole scope is reconciled", () => {
    const trust: TrustBlock = { status: "broker-confirmed", confirmed_days: 3, total_days: 3, label: "broker-confirmed" };
    render(<TrustBadge trust={trust} />);
    expect(screen.getByTestId("trust-badge")).toHaveTextContent("broker-confirmed ✓");
  });

  it("shows the bot-computed N/M count when part of the scope is unreconciled", () => {
    const trust: TrustBlock = { status: "bot-computed", confirmed_days: 22, total_days: 23, label: "22/23 days broker-confirmed" };
    render(<TrustBadge trust={trust} />);
    const badge = screen.getByTestId("trust-badge");
    expect(badge).toHaveTextContent("bot-computed");
    expect(badge).toHaveTextContent("22/23 days broker-confirmed");
  });
});

describe("PaperBanner (SIM-05)", () => {
  it("renders in paper mode", () => {
    render(<PaperBanner mode="paper" />);
    expect(screen.getByTestId("paper-banner")).toBeInTheDocument();
  });

  it("renders nothing in live mode", () => {
    const { container } = render(<PaperBanner mode="live" />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("CsvButton (RPT-10)", () => {
  it("links straight to /reports/csv with the right table and period params", () => {
    render(<CsvButton table="entries" period={{ month: "2026-07" }} />);
    const link = screen.getByTestId("csv-entries");
    expect(link).toHaveAttribute("href", "/reports/csv?table=entries&month=2026-07");
    expect(link).toHaveAttribute("download");
  });

  it("omits period params entirely for all-time (no narrowing param)", () => {
    render(<CsvButton table="daily" period={{}} />);
    expect(screen.getByTestId("csv-daily")).toHaveAttribute("href", "/reports/csv?table=daily");
  });
});
