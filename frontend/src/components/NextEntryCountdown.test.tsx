// UI-24: the next-entry countdown is display-only (UI-03) — it renders exactly
// what /day/status reports and never decides anything itself.
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api";
import { NextEntryCountdown } from "./NextEntryCountdown";
import type { DayStatus } from "../types";

function status(over: Partial<DayStatus> = {}): DayStatus {
  return { started: true, running: true, armed: true, ...over };
}

beforeEach(() => {
  vi.restoreAllMocks();
  // Freeze Date ONLY (timers/promises stay real, so findBy* still works):
  // the day-label logic compares the entry's ET date with "today", which must
  // not depend on when the suite happens to run. Thu 2026-07-09 12:32:55 ET.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-07-09T16:32:55Z"));
});

afterEach(() => {
  vi.useRealTimers();
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

  // DAY-01/UI-24 (operator ruling 2026-07-11): on a weekend or market holiday
  // the backend rolls next_entry_at to the next trading day; the strip must
  // name the day and count down across the gap — a Saturday reader must never
  // believe an entry fires "in 7:03:05" today.
  it("labels the day and counts down in days when the next entry is not today", async () => {
    vi.setSystemTime(new Date("2026-07-11T13:53:00Z")); // Sat 2026-07-11 09:53 ET
    vi.spyOn(api, "getDayStatus").mockResolvedValue(
      status({
        next_entry_at: "2026-07-13T11:56:00-04:00",     // Monday's first entry
        seconds_to_next: 2 * 86400 + 2 * 3600 + 3 * 60, // 2d 2h 3m
        entries_remaining: 6,
      }),
    );
    render(<NextEntryCountdown />);

    const el = await screen.findByTestId("next-entry");
    expect(el).toHaveTextContent("Next entry Mon 11:56 ET");
    expect(el).toHaveTextContent("in 2d 2:03:00");
  });
});
