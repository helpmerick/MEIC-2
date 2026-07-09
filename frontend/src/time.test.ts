import { describe, expect, it } from "vitest";
import { canonicalTime, etToZone, isMilitaryTime, withinMarketHours, zoneLabel } from "./time";

// ET entry times shown in the operator's local zone. We pass an explicit zone so
// the assertion is deterministic regardless of the CI box's timezone; the DST
// correctness comes from Intl resolving the right offset for the given instant.
describe("etToZone — ET entry time in the operator's local zone", () => {
  it("converts an ET time to London (5h ahead of New York year-round)", () => {
    // 11:53 ET -> 16:53 London, in either DST regime (EDT/BST or EST/GMT).
    expect(etToZone("11:53", "Europe/London")).toBe("16:53");
  });

  it("accepts a single-digit hour", () => {
    expect(etToZone("9:32", "Europe/London")).toBe("14:32");
  });

  it("handles a zone behind New York (Los Angeles, 3h back)", () => {
    expect(etToZone("11:53", "America/Los_Angeles")).toBe("08:53");
  });

  it("is identity when the target zone IS New York", () => {
    expect(etToZone("15:30", "America/New_York")).toBe("15:30");
  });

  it("accepts a UK-style dot separator (11.53 == 11:53)", () => {
    expect(etToZone("11.53", "Europe/London")).toBe("16:53");
  });

  it("returns null for a non-24-hour input", () => {
    expect(etToZone("11-53", "Europe/London")).toBeNull();
    expect(etToZone("25:00", "Europe/London")).toBeNull();
    expect(etToZone("", "Europe/London")).toBeNull();
  });
});

describe("isMilitaryTime — 24-hour HH:MM, colon or dot", () => {
  it.each(["09:32", "9:32", "11.53", "00:00", "23:59", "15:30"])("accepts %s", (t) => {
    expect(isMilitaryTime(t)).toBe(true);
  });
  it.each(["1:53pm", "24:00", "11:60", "0930", "11-53", "noon", ""])("rejects %s", (t) => {
    expect(isMilitaryTime(t)).toBe(false);
  });
});

describe("canonicalTime — normalise to HH:MM", () => {
  it.each([["11.53", "11:53"], ["9:32", "09:32"], ["9.5", "9.5"], ["15:30", "15:30"]])(
    "%s -> %s", (input, out) => {
      expect(canonicalTime(input)).toBe(out);
    });
});

describe("withinMarketHours — 09:30-16:00 ET", () => {
  it.each(["09:30", "10:00", "15:30", "16:00"])("accepts open-hours %s", (t) => {
    expect(withinMarketHours(t)).toBe(true);
  });
  it.each(["09:29", "08:00", "16:01", "16:30"])("rejects closed-hours %s", (t) => {
    expect(withinMarketHours(t)).toBe(false);
  });
});

describe("zoneLabel", () => {
  it("shortens an IANA zone to its city", () => {
    expect(zoneLabel("Europe/London")).toBe("London");
    expect(zoneLabel("America/New_York")).toBe("New York");
    expect(zoneLabel("UTC")).toBe("UTC");
  });
});
