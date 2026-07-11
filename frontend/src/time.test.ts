import { describe, expect, it } from "vitest";
import { canonicalTime, etToZone, instantToZone, isMilitaryTime, withinMarketHours, zoneLabel } from "./time";

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

// UI-24 rollover / TC-DAY-07 (DAY-01a era): /day/status's next_entry_at is a
// FULL ISO instant, so an entry on the far side of a DST switch converts with
// the instant's OWN offset — never today's.
describe("instantToZone — full-instant conversion (DST-correct across the switch)", () => {
  it("converts a January instant with the instant's own offset, not today's", () => {
    // Mon 2027-01-04 11:56 ET is EST (-05:00) = 16:56 UTC — London is on GMT.
    expect(instantToZone("2027-01-04T11:56:00-05:00", "Europe/London")).toBe("16:56");
    // The same ET wall-clock in July (EDT, -04:00) is a DIFFERENT instant
    // (15:56 UTC) — London is on BST, so the echo happens to read the same…
    expect(instantToZone("2026-07-13T11:56:00-04:00", "Europe/London")).toBe("16:56");
    // …but a zone that does NOT observe DST exposes the raw instants: the two
    // "11:56 ET" entries land 60 minutes apart in Phoenix. Converting with
    // "today's offset" could never produce both.
    expect(instantToZone("2027-01-04T11:56:00-05:00", "America/Phoenix")).toBe("09:56");
    expect(instantToZone("2026-07-13T11:56:00-04:00", "America/Phoenix")).toBe("08:56");
  });

  it("returns null for an unparsable instant (never a fabricated echo)", () => {
    expect(instantToZone("not-a-date", "Europe/London")).toBeNull();
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
