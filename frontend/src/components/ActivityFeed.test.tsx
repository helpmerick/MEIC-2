// Day-separator feature (operator request 2026-07-15): the ACTIVITY feed must
// clearly separate days — a divider row whenever consecutive items fall on
// different ET trading days (DAY-03).
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ActivityFeed } from "./ActivityFeed";
import type { ActivityLine } from "../types";

function line(overrides: Partial<ActivityLine> = {}): ActivityLine {
  return { icon: "✅", label: "Entry filled", entry: "", detail: "", ...overrides };
}

describe("ActivityFeed day separators", () => {
  it("renders a separator row before the first item of each new day, newest-first", () => {
    const activity: ActivityLine[] = [
      line({ label: "Entry filled", entry: "2026-07-14#1" }),
      line({ label: "Stop placed", entry: "2026-07-14#1" }),
      line({ label: "Day armed", date: "2026-07-13" }),
    ];
    render(<ActivityFeed activity={activity} />);

    const feed = screen.getByRole("list");
    const rows = Array.from(feed.children).map((el) => el.textContent);
    expect(rows[0]).toMatch(/Tuesday 14 July 2026/);
    expect(rows[3]).toMatch(/Monday 13 July 2026/);
    // exactly one separator per day boundary, not one per item
    const separators = feed.querySelectorAll(".feed-day-separator");
    expect(separators).toHaveLength(2);
  });

  it("does not render a separator when every item is the same day", () => {
    const activity: ActivityLine[] = [
      line({ label: "a", entry: "2026-07-14#1" }),
      line({ label: "b", entry: "2026-07-14#2" }),
    ];
    render(<ActivityFeed activity={activity} />);
    expect(screen.getByRole("list").querySelectorAll(".feed-day-separator")).toHaveLength(1);
  });

  it("regroups correctly when a new item for a NEW day arrives at the top (live update)", () => {
    const day1: ActivityLine[] = [
      line({ label: "a", entry: "2026-07-14#1" }),
      line({ label: "b", entry: "2026-07-14#2" }),
    ];
    const { rerender } = render(<ActivityFeed activity={day1} />);
    expect(screen.getByRole("list").querySelectorAll(".feed-day-separator")).toHaveLength(1);

    const day2: ActivityLine[] = [
      line({ label: "c", entry: "2026-07-15#1" }), // arrives via ws/poll, newest-first
      ...day1,
    ];
    rerender(<ActivityFeed activity={day2} />);

    const feed = screen.getByRole("list");
    const rows = Array.from(feed.children).map((el) => el.textContent);
    expect(rows[0]).toMatch(/Wednesday 15 July 2026/);
    expect(rows[1]).toBe("✅c2026-07-15#1"); // "c"'s line rendered via icon/label/entry
    expect(feed.querySelectorAll(".feed-day-separator")).toHaveLength(2);
  });

  it("the separator row is not clickable / not a feed-line, and carries no icon or entry", () => {
    const activity: ActivityLine[] = [line({ entry: "2026-07-14#1" })];
    render(<ActivityFeed activity={activity} />);
    const sep = screen.getByText(/Tuesday 14 July 2026/);
    expect(sep).toHaveClass("feed-day-separator");
    expect(sep.querySelector(".feed-icon")).toBeNull();
    expect(sep.querySelector(".feed-entry")).toBeNull();
  });

  // UI-31 (v1.73, queue slice 5): the separator must stay visible while the
  // feed scrolls -- `.feed` is the scrolling ancestor and the separator is
  // its direct <li> child, so sticky needs no other container fix. Asserted
  // via the INLINE style (not the styles.css rule) because vitest.config.ts
  // runs with `css: false` -- an external stylesheet never reaches jsdom.
  it("positions the day separator sticky within the feed's own scroll container", () => {
    const activity: ActivityLine[] = [line({ entry: "2026-07-14#1" })];
    render(<ActivityFeed activity={activity} />);
    const sep = screen.getByText(/Tuesday 14 July 2026/);
    expect(sep.style.position).toBe("sticky");
    expect(sep.style.top).toBe("0px");
  });
});

describe("ActivityFeed per-row ET time (UI-31)", () => {
  it("shows the row's own ET wall-clock time, derived from its `at` instant via instantToZone", () => {
    // 2026-07-14T15:31:00Z is 11:31 ET (EDT, UTC-4 in July) -- the same
    // conversion the day-grouping logic already trusts (time.ts).
    const activity: ActivityLine[] = [
      line({ label: "Entry filled", entry: "2026-07-14#1", at: "2026-07-14T15:31:00Z" }),
    ];
    render(<ActivityFeed activity={activity} />);
    expect(screen.getByText("11:31")).toHaveClass("feed-time");
  });

  it("shows nothing when the row has no `at` -- never fabricates a time", () => {
    const activity: ActivityLine[] = [line({ label: "Day armed", date: "2026-07-13" })];
    render(<ActivityFeed activity={activity} />);
    const row = screen.getByText("Day armed").closest("li");
    expect(row?.querySelector(".feed-time")).toBeNull();
  });
});

describe("ActivityFeed hover tooltips explain every event (UI-31, TC-UI-09)", () => {
  it("a known event type gets a styled, focus- and tap-capable tooltip -- never a native title", async () => {
    const activity: ActivityLine[] = [
      line({ label: "Long sold (LEX)", entry: "2026-07-14#1", type: "LongSold" }),
    ];
    render(<ActivityFeed activity={activity} />);
    const row = screen.getByText("Long sold (LEX)").closest("li")!;
    expect(row.querySelector("[title]")).toBeNull();

    const trigger = within(row).getByRole("button", { name: /explain: long sold \(lex\)/i });
    fireEvent.click(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent(/long exit/i);
  });

  it("a row with no recognised type renders no tooltip -- never a fabricated explanation", () => {
    const activity: ActivityLine[] = [line({ label: "Mystery event", entry: "2026-07-14#1" })];
    render(<ActivityFeed activity={activity} />);
    const row = screen.getByText("Mystery event").closest("li")!;
    expect(within(row).queryByRole("button", { name: /explain:/i })).toBeNull();
  });
});
