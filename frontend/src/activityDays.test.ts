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

  it("the FIRST feed item with a resolvable day gets a header above it (DayArmed at position 0)", () => {
    const rows = groupActivityByDay([line({ label: "Day armed", date: "2026-07-14" })]);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ kind: "separator", day: "2026-07-14" });
    expect(rows[1]).toMatchObject({ kind: "item" });
  });

  it("a leading run with NO derivable day stays at the top, headerless — never a fabricated date", () => {
    const rows = groupActivityByDay([
      line({ label: "mode-switch", at: null, date: null, entry: "" }),
      line({ label: "a", date: "2026-07-14" }),
    ]);
    expect(rows.map((r) => (r.kind === "separator" ? `SEP:${r.day}` : r.item.label))).toEqual([
      "mode-switch", "SEP:2026-07-14", "a",
    ]);
  });
});

describe("backdated `at` never fragments a day (production bug, 2026-07-15)", () => {
  // The journal is write-time monotonic but the EOD look-back captures
  // settlements a day LATE, stamping `at` with the broker's own settlement
  // instant -- so day-of-item is NOT monotonic in journal order. Operator
  // requirement, verbatim: "Dates should always be continuous."

  it("renders the exact production sequence from the operator's screenshot with one header per day", () => {
    // Journal order, newest-first, exactly as /activity returned it:
    const activity: ActivityLine[] = [
      // journaled 07-14: side expired worthless (at = 07-14 ET)
      line({ label: "Side expired worthless", entry: "2026-07-13#2", at: "2026-07-14T20:15:00Z" }),
      // journaled 07-14 by the EOD look-back, but BACKDATED to the broker's
      // settlement instant on 07-13 (20:00Z == 16:00 ET, still 07-13 ET)
      line({ label: "Settlement recorded (broker)", entry: "2026-07-13#2", at: "2026-07-13T20:00:00Z" }),
      line({ label: "Settlement recorded (broker)", entry: "2026-07-13#2", at: "2026-07-13T20:00:00Z" }),
      line({ label: "Side closed", entry: "2026-07-14#101", at: "2026-07-14T18:00:00Z" }),
    ];
    const rows = groupActivityByDay(activity);
    expect(rows.map((r) => (r.kind === "separator" ? `SEP:${r.day}` : r.item.label))).toEqual([
      "SEP:2026-07-14",
      "Side expired worthless",
      "Side closed",
      "SEP:2026-07-13",
      "Settlement recorded (broker)",
      "Settlement recorded (broker)",
    ]);
  });

  it("emits EXACTLY one separator per distinct day across a 25-item feed with interleaved days", () => {
    // Deterministically interleave three days through 25 items, the way
    // backdated look-back captures scatter them through the journal.
    const days = ["2026-07-14", "2026-07-13", "2026-07-12"];
    const activity: ActivityLine[] = Array.from({ length: 25 }, (_, i) =>
      line({ label: `item-${i}`, at: `${days[(i * 7) % 3]}T18:0${i % 10}:00Z` }),
    );
    const rows = groupActivityByDay(activity);

    // uniqueness: one separator per distinct day, no repeats, newest first
    const seps = rows.filter((r) => r.kind === "separator").map((r) => r.day);
    expect(new Set(seps).size).toBe(seps.length);
    expect(seps).toEqual(["2026-07-14", "2026-07-13", "2026-07-12"]);
    // continuity: every item under a header belongs to that header's day
    let current: string | null = null;
    for (const r of rows) {
      if (r.kind === "separator") { current = r.day; continue; }
      expect(r.item.at!.startsWith(current!)).toBe(true);
    }
    // nothing dropped, nothing duplicated
    expect(rows.filter((r) => r.kind === "item")).toHaveLength(25);
  });

  it("preserves relative journal order WITHIN each day (stable sort)", () => {
    const activity: ActivityLine[] = [
      line({ label: "n1", at: "2026-07-14T20:00:00Z" }),
      line({ label: "o1", at: "2026-07-13T20:00:00Z" }), // backdated
      line({ label: "n2", at: "2026-07-14T18:00:00Z" }),
      line({ label: "o2", at: "2026-07-13T19:00:00Z" }), // backdated
    ];
    const rows = groupActivityByDay(activity);
    expect(rows.map((r) => (r.kind === "separator" ? `SEP:${r.day}` : r.item.label))).toEqual([
      "SEP:2026-07-14", "n1", "n2", "SEP:2026-07-13", "o1", "o2",
    ]);
  });
});
