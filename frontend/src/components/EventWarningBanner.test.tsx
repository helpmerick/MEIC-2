// CAL-11/UI-34 (v1.84, doc 11) — the Trading tab's event-proximity warning
// banner. These tests pin what it RENDERS from GET /calendar/warnings and
// what it SENDS on dismiss, never that it decides anything itself (UI-03):
// it is purely informational, never a gate/entry input.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import { EventWarningBanner } from "./EventWarningBanner";
import type { CalendarWarnings, EventWarning } from "../types";

function warningFixture(overrides: Partial<EventWarning> = {}): EventWarning {
  return {
    category: "FOMC",
    event_date: "2026-07-29",
    proximity_tier: 2,
    label: "FOMC",
    tier: 1,
    best_effort: false,
    computed: false,
    human_label: "FOMC in 2 trading days (Wed)",
    ...overrides,
  };
}

function warningsFixture(warnings: EventWarning[]): CalendarWarnings {
  return { available: true, warnings };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("event proximity warning banner (CAL-11/UI-34)", () => {
  it("the event warning banner renders amber, never the red critical banner class", async () => {
    vi.spyOn(api, "getCalendarWarnings").mockResolvedValue(warningsFixture([warningFixture()]));
    render(<EventWarningBanner />);

    const banner = await screen.findByTestId("event-warning-banner");
    expect(banner).toHaveClass("event-warning-banner");
    // UI-34's ratified constraint: amber, structurally distinct from the
    // STP-04/UNPROTECTED critical RED banner -- must never carry that class.
    expect(banner).not.toHaveClass("banner-error");
    expect(banner.className).not.toMatch(/\bbad\b/);
  });

  it("renders nothing when there are no warnings (no empty banner shell)", async () => {
    vi.spyOn(api, "getCalendarWarnings").mockResolvedValue(warningsFixture([]));
    render(<EventWarningBanner />);

    await waitFor(() => expect(api.getCalendarWarnings).toHaveBeenCalled());
    expect(screen.queryByTestId("event-warning-banner")).not.toBeInTheDocument();
  });

  it("renders the backend-composed human_label verbatim, in the given (nearest-first) order", async () => {
    vi.spyOn(api, "getCalendarWarnings").mockResolvedValue(warningsFixture([
      warningFixture({ proximity_tier: 0, human_label: "Today is FOMC" }),
      warningFixture({ category: "CPI", event_date: "2026-08-01", proximity_tier: 3, human_label: "CPI in 3 trading days (Fri)" }),
    ]));
    render(<EventWarningBanner />);

    const banner = await screen.findByTestId("event-warning-banner");
    const rows = within(banner).getAllByText(/^(Today is FOMC|CPI in 3 trading days \(Fri\))$/);
    expect(rows[0]).toHaveTextContent("Today is FOMC");
    expect(rows[1]).toHaveTextContent("CPI in 3 trading days (Fri)");
  });

  it("dismissing the T-3 tier removes only that row; T-2/T-1/T-0 rows for the same event still render", async () => {
    const full = [
      warningFixture({ proximity_tier: 3, human_label: "FOMC in 3 trading days (Tue)" }),
      warningFixture({ proximity_tier: 2, human_label: "FOMC in 2 trading days (Wed)" }),
      warningFixture({ proximity_tier: 1, human_label: "FOMC in 1 trading day (Thu)" }),
      warningFixture({ proximity_tier: 0, human_label: "Today is FOMC" }),
    ];
    const getWarnings = vi.spyOn(api, "getCalendarWarnings");
    getWarnings.mockResolvedValueOnce(warningsFixture(full));
    const dismissSpy = vi.spyOn(api, "dismissCalendarWarning").mockResolvedValue({ result: "dismissed" });

    render(<EventWarningBanner />);
    await screen.findByTestId("event-warning-banner");

    // The honest re-fetch after dismiss reflects the backend's OWN persisted
    // (event, tier) dismissal — the T-3 row alone drops out.
    getWarnings.mockResolvedValueOnce(warningsFixture(full.filter((w) => w.proximity_tier !== 3)));

    const t3Row = screen.getByTestId("event-warning-row-FOMC-2026-07-29-3");
    fireEvent.click(within(t3Row).getByRole("button", { name: /dismiss warning/i }));

    await waitFor(() => expect(dismissSpy).toHaveBeenCalledWith("FOMC", "2026-07-29", 3));
    await waitFor(() =>
      expect(screen.queryByTestId("event-warning-row-FOMC-2026-07-29-3")).not.toBeInTheDocument());

    expect(screen.getByTestId("event-warning-row-FOMC-2026-07-29-2")).toBeInTheDocument();
    expect(screen.getByTestId("event-warning-row-FOMC-2026-07-29-1")).toBeInTheDocument();
    expect(screen.getByTestId("event-warning-row-FOMC-2026-07-29-0")).toBeInTheDocument();
  });

  it("a tier-2 Fed-speaker warning is labeled best-effort, never stated as certain", async () => {
    vi.spyOn(api, "getCalendarWarnings").mockResolvedValue(warningsFixture([
      warningFixture({
        category: "FED_SPEAKER", label: "FED_SPEAKER", tier: 2, best_effort: true,
        human_label: "FED_SPEAKER in 1 trading day (Thu)",
      }),
    ]));
    render(<EventWarningBanner />);

    const row = await screen.findByTestId("event-warning-row-FED_SPEAKER-2026-07-29-2");
    expect(row).toHaveTextContent(/best-effort/i);
    expect(row).not.toHaveTextContent(/certain|confirmed/i);
  });

  it("a dismiss call never touches any entry/gate state", async () => {
    const trackedKeys = (Object.keys(api) as (keyof typeof api)[]).filter(
      (k) => typeof api[k] === "function" && k !== "getCalendarWarnings" && k !== "dismissCalendarWarning",
    );
    const otherSpies = trackedKeys.map((k) => vi.spyOn(api, k));
    vi.spyOn(api, "getCalendarWarnings").mockResolvedValue(warningsFixture([warningFixture()]));
    const dismissSpy = vi.spyOn(api, "dismissCalendarWarning").mockResolvedValue({ result: "dismissed" });

    render(<EventWarningBanner />);
    const row = await screen.findByTestId("event-warning-row-FOMC-2026-07-29-2");
    fireEvent.click(within(row).getByRole("button", { name: /dismiss warning/i }));

    await waitFor(() => expect(dismissSpy).toHaveBeenCalled());
    for (const spy of otherSpies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });
});
