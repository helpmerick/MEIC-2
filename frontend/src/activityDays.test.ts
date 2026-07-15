import { afterEach, describe, expect, it, vi } from "vitest";

import { dayOfActivityItem, formatDaySeparator, groupActivityByDay } from "./activityDays";
import type { ActivityLine } from "./types";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

function line(overrides: Partial<ActivityLine> = {}): ActivityLine {
  return { icon: "✅", label: "Entry filled", entry: "", detail: "", ...overrides };
}

describe("dayOfActivityItem (day-separator feature, 2026-07-15)", () => {
  it("derives the ET day from `at`, never the browser's local date (DAY-03)", () => {
    // 23:30 ET on 2026-07-14 == 03:30 UTC on 2026-07-15 -- the UTC date and
    // the ET date disagree here, which is exactly the boundary this must get
    // right.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.stubEnv("TZ", "Pacific/Kiritimati"); // a browser 14h ahead of UTC
    const item = line({ at: "2026-07-15T03:30:00Z" });
    expect(dayOfActivityItem(item)).toBe("2026-07-14");
  });

  it("falls back to the event's own `date` field when there is no `at` (DayArmed)", () => {
    const item = line({ at: null, date: "2026-07-14" });
    expect(dayOfActivityItem(item)).toBe("2026-07-14");
  });

  it("falls back to the entry_id's ET-day-stamped prefix when neither `at` nor `date` is present", () => {
    const item = line({ at: null, date: null, entry: "2026-07-14#101" });
    expect(dayOfActivityItem(item)).toBe("2026-07-14");
  });

  it("returns null (never a fabricated date) when nothing usable is present", () => {
    const item = line({ at: null, date: null, entry: "" });
    expect(dayOfActivityItem(item)).toBeNull();
  });

  it("ignores an unparsable `at` and falls through the chain", () => {
    const item = line({ at: "not-a-date", date: "2026-07-14" });
    expect(dayOfActivityItem(item)).toBe("2026-07-14");
  });
});

describe("formatDaySeparator", () => {
  it("renders a full weekday/date label", () => {
    expect(formatDaySeparator("2026-07-14")).toBe("Tuesday 14 July 2026");
  });
});

describe("groupActivityByDay (newest-first feed)", () => {
  it("inserts a separator before the first item of each new day", () => {
    const activity: ActivityLine[] = [
      line({ label: "a", date: "2026-07-14" }),
      line({ label: "b", date: "2026-07-14" }),
      line({ label: "c", date: "2026-07-13" }),
      line({ label: "d", date: "2026-07-13" }),
    ];
    const rows = groupActivityByDay(activity);
    expect(rows.map((r) => (r.kind === "separator" ? `SEP:${r.day}` : r.item.label))).toEqual([
      "SEP:2026-07-14", "a", "b", "SEP:2026-07-13", "c", "d",
    ]);
  });

  it("an item with no derivable day inherits the PRECEDING item's day, no spurious separator", () => {
    const activity: ActivityLine[] = [
      line({ label: "a", date: "2026-07-14" }),
      line({ label: "mode-switch", at: null, date: null, entry: "" }), // ModeSwitchStaged shape
      line({ label: "b", date: "2026-07-14" }),
    ];
    const rows = groupActivityByDay(activity);
    expect(rows.map((r) => (r.kind === "separator" ? `SEP:${r.day}` : r.item.label))).toEqual([
      "SEP:2026-07-14", "a", "mode-switch", "b",
    ]);
  });

  it("a brand-new day's first item (live arrival at the top) gets its own separator", () => {
    const before: ActivityLine[] = [
      line({ label: "a", date: "2026-07-14" }),
      line({ label: "b", date: "2026-07-14" }),
    ];
    const after: ActivityLine[] = [
      line({ label: "new", date: "2026-07-15" }), // arrives via ws/poll, newest-first
      ...before,
    ];
    const rows = groupActivityByDay(after);
    expect(rows.map((r) => (r.kind === "separator" ? `SEP:${r.day}` : r.item.label))).toEqual([
      "SEP:2026-07-15", "new", "SEP:2026-07-14", "a", "b",
    ]);
  });
});
