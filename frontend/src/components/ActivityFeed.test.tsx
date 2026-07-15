// Day-separator feature (operator request 2026-07-15): the ACTIVITY feed must
// clearly separate days — a divider row whenever consecutive items fall on
// different ET trading days (DAY-03).
import { render, screen } from "@testing-library/react";
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
});
