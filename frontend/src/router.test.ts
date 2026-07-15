import { describe, expect, it } from "vitest";
import { parseHash, resultsDayHref } from "./router";

// UI-27: a tiny hash router — no library installed. These pin what each hash
// resolves to; App.test.tsx covers the reactive nav-click/deep-link behavior.
describe("parseHash (UI-27)", () => {
  it("defaults to Trading for an empty hash", () => {
    expect(parseHash("")).toEqual({ page: "trading" });
  });

  it("defaults to Trading for a bare '#'", () => {
    expect(parseHash("#")).toEqual({ page: "trading" });
  });

  it("resolves #/results to the Results overview", () => {
    expect(parseHash("#/results")).toEqual({ page: "results" });
  });

  it("resolves #/results/day/YYYY-MM-DD to a day drill-down deep link", () => {
    expect(parseHash("#/results/day/2026-07-10")).toEqual({
      page: "results-day",
      date: "2026-07-10",
    });
  });

  it("resolves #/calendar to the Calendar tab (CAL-08/UI-30)", () => {
    expect(parseHash("#/calendar")).toEqual({ page: "calendar" });
  });

  it("resolves #/how-it-works to the How-it-works tab (DOC-05/UI-29)", () => {
    expect(parseHash("#/how-it-works")).toEqual({ page: "how-it-works" });
  });

  it("falls back to Trading for an unrecognized hash", () => {
    expect(parseHash("#/nonsense")).toEqual({ page: "trading" });
  });

  it("rejects a malformed day (not YYYY-MM-DD) and falls back to Trading", () => {
    expect(parseHash("#/results/day/not-a-date")).toEqual({ page: "trading" });
  });
});

describe("resultsDayHref", () => {
  it("builds the deep-link hash for a given ISO date", () => {
    expect(resultsDayHref("2026-07-10")).toBe("#/results/day/2026-07-10");
  });
});
