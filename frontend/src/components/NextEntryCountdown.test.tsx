// UI-24: the next-entry countdown is display-only (UI-03) — it renders exactly
// what /day/status reports and never decides anything itself.
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import { NextEntryCountdown } from "./NextEntryCountdown";
import type { DayStatus } from "../types";

function status(over: Partial<DayStatus> = {}): DayStatus {
  return { started: true, running: true, armed: true, ...over };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("NextEntryCountdown", () => {
  it("shows the next entry's ET time and a ticking countdown when armed", async () => {
    vi.spyOn(api, "getDayStatus").mockResolvedValue(
      status({ next_entry_at: "2026-07-09T12:35:00-04:00", seconds_to_next: 125, entries_remaining: 1 }),
    );
    render(<NextEntryCountdown />);

    const el = await screen.findByTestId("next-entry");
    expect(el).toHaveTextContent("Next entry 12:35 ET");
    expect(el).toHaveTextContent("in 2:05");
  });

  it('shows "schedule idle — arm to run" when disarmed, with no countdown', async () => {
    vi.spyOn(api, "getDayStatus").mockResolvedValue(status({ armed: false, next_entry_at: null }));
    render(<NextEntryCountdown />);

    const el = await screen.findByTestId("next-entry");
    expect(el).toHaveTextContent("schedule idle — arm to run");
  });

  it('shows "no more entries today" when armed with nothing left', async () => {
    vi.spyOn(api, "getDayStatus").mockResolvedValue(
      status({ armed: true, next_entry_at: null, entries_remaining: 0 }),
    );
    render(<NextEntryCountdown />);

    const el = await screen.findByTestId("next-entry");
    expect(el).toHaveTextContent("no more entries today");
  });
});
