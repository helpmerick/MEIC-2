// CAL-08/UI-30 (v1.71): the Calendar tab holds NO trading logic (UI-03) —
// these tests pin what it RENDERS from the GET /calendar read model and what
// it SENDS on each operator action, never that it decides anything itself.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api";
import { CalendarPage } from "./CalendarPage";
import type { CalendarData } from "../types";

function calendarFixture(overrides: Partial<CalendarData> = {}): CalendarData {
  return {
    available: true,
    tags: {},
    staleness: {},
    standing_rules: {},
    ...overrides,
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

describe("year-grid rendering (CAL-08)", () => {
  it("renders all 12 months of the current ET year, scrollable", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture());
    render(<CalendarPage />);

    const strip = await screen.findByTestId("calendar-months");
    for (let m = 1; m <= 12; m++) {
      expect(within(strip).getByTestId(`calendar-month-2026-${String(m).padStart(2, "0")}`)).toBeInTheDocument();
    }
  });

  it("marks TODAY using the ET calendar date, never the browser's local date (DAY-03)", async () => {
    // The system clock sits at 2026-07-15 18:00 UTC == 14:00 ET (EDT) that
    // same day, but a browser sitting in a zone 14h AHEAD of UTC
    // (Pacific/Kiritimati) would already read its OWN local date as
    // 2026-07-16. If the grid ever highlighted "today" off the browser's
    // local date, it would light up the 16th, one day into the future --
    // the ET grid must light up the 15th instead.
    // Fake ONLY the Date constructor -- testing-library's internal findBy/
    // waitFor polling still uses REAL setTimeout, so it isn't stalled.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-07-15T18:00:00Z"));
    vi.stubEnv("TZ", "Pacific/Kiritimati");
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture());

    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    expect(screen.getByTestId("cal-day-2026-07-15")).toHaveClass("today");
    expect(screen.queryByTestId("cal-day-2026-07-16")).not.toHaveClass("today");
  });

  it("shows an honest note instead of blocking when the calendar isn't wired", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue({ available: false });
    render(<CalendarPage />);
    expect(await screen.findByTestId("calendar-unwired")).toHaveTextContent(/not wired/i);
  });
});

describe("tier-2 events are visually distinct — structurally, not by colour alone (CAL-01/UI-26)", () => {
  it("renders a DIFFERENT class and shape glyph for a tier-2 event than a tier-1 one", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      staleness: {
        FOMC: { imported_at: "2026-07-01T00:00:00+00:00", horizon: "2026-12-16", stale: false, tier: 1, dates: ["2026-07-29"] },
        FED_SPEAKER: { imported_at: "2026-07-01T00:00:00+00:00", horizon: "2026-07-20", stale: false, tier: 2, dates: ["2026-07-20"] },
      },
    }));
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    const tier1Mark = screen.getByTestId("cal-evt-tier1-2026-07-29");
    const tier2Mark = screen.getByTestId("cal-evt-tier2-2026-07-20");
    expect(tier1Mark.className).not.toBe(tier2Mark.className);
    // Different glyphs (shape), not merely a different colour on the same glyph.
    expect(tier1Mark.textContent).not.toBe(tier2Mark.textContent);
  });

  it("a day with no import shows no fabricated event marker", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      staleness: {
        FOMC: { imported_at: "2026-07-01T00:00:00+00:00", horizon: "2026-07-29", stale: false, tier: 1, dates: ["2026-07-29"] },
      },
    }));
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    expect(screen.queryByTestId("cal-evt-tier1-2026-07-30")).not.toBeInTheDocument();
  });

  it("tolerates a staleness row WITHOUT the additive `dates` field — tab renders, no markers, no crash", async () => {
    // Final review (2026-07-15): `dates` is slice-2 ADDITIVE; a backend that
    // predates it omits the field entirely. The tab must render (grid,
    // staleness rows) with simply no event markers for that category —
    // never a thrown iteration that blanks the whole page.
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      staleness: {
        FOMC: { imported_at: "2026-07-01T00:00:00+00:00", horizon: "2026-07-29", stale: false, tier: 1 },
      },
    }));
    render(<CalendarPage />);

    expect(await screen.findByTestId("calendar-months")).toBeInTheDocument();
    expect(screen.getByTestId("staleness-FOMC")).toHaveTextContent("2026-07-29");
    expect(screen.queryByTestId("cal-evt-tier1-2026-07-29")).not.toBeInTheDocument();
  });
});

describe("tagged days are unmistakably marked (CAL-03/CAL-08)", () => {
  it("renders a distinct marker + class for a manually-tagged day", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      tags: { "2026-07-15": { label: "FOMC", origin: "manual", category: null } },
    }));
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    const cell = screen.getByTestId("cal-day-2026-07-15");
    expect(cell).toHaveClass("tagged");
    expect(cell).toHaveClass("origin-manual");
    expect(cell).toHaveAccessibleName(/tagged NO-TRADE: FOMC \(manual\)/i);
  });

  it("an auto-tagged day (standing rule) carries a distinct origin class from a manual tag", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      tags: { "2026-07-29": { label: "FOMC", origin: "auto", category: "FOMC" } },
    }));
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    const cell = screen.getByTestId("cal-day-2026-07-29");
    expect(cell).toHaveClass("origin-auto");
    expect(cell).not.toHaveClass("origin-manual");
  });
});

describe("staleness banner (CAL-02) — displayed, never blocking", () => {
  it("shows every known category, including ones never imported at all", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture());
    render(<CalendarPage />);

    const banner = await screen.findByTestId("calendar-staleness");
    expect(within(banner).getByTestId("staleness-FOMC")).toHaveTextContent("no data imported");
    expect(within(banner).getByTestId("staleness-FED_SPEAKER")).toHaveTextContent("no data imported");
  });

  it("shows the coverage horizon for an imported category", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      staleness: { CPI: { imported_at: "2026-06-01T00:00:00+00:00", horizon: "2026-12-11", stale: false, tier: 1, dates: [] } },
    }));
    render(<CalendarPage />);

    expect(await screen.findByTestId("staleness-CPI")).toHaveTextContent("2026-12-11");
  });

  it("banners an import older than cal_stale_after_days as stale, without blocking anything", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      staleness: { CPI: { imported_at: "2026-01-01T00:00:00+00:00", horizon: "2026-03-11", stale: true, tier: 1, dates: [] } },
    }));
    render(<CalendarPage />);

    const row = await screen.findByTestId("staleness-CPI");
    expect(row).toHaveTextContent(/stale/i);
    expect(row.className).toContain("stale");
  });
});

describe("click a day → events, tag/untag, label editor (CAL-03/08)", () => {
  it("shows no events and offers to tag an untagged, dataless day", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture());
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    fireEvent.click(screen.getByTestId("cal-day-2026-07-16"));
    const detail = await screen.findByTestId("calendar-day-detail");
    expect(detail).toHaveTextContent("No imported events on this day.");
    expect(within(detail).getByRole("button", { name: /tag no-trade/i })).toBeInTheDocument();
  });

  it("tags a day via the label editor, posting the typed label", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture());
    const tagSpy = vi.spyOn(api, "tagCalendarDay").mockResolvedValue({ result: "tagged", day: "2026-07-16", label: "CPI print" });
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    fireEvent.click(screen.getByTestId("cal-day-2026-07-16"));
    await screen.findByTestId("calendar-day-detail");
    fireEvent.change(screen.getByLabelText("tag label"), { target: { value: "CPI print" } });
    fireEvent.click(screen.getByRole("button", { name: /tag no-trade/i }));

    await waitFor(() => expect(tagSpy).toHaveBeenCalledWith("2026-07-16", "CPI print"));
  });

  it("lists the day's imported events with their tier", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      staleness: { FOMC: { imported_at: "t", horizon: "2026-07-29", stale: false, tier: 1, dates: ["2026-07-29"] } },
    }));
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    fireEvent.click(screen.getByTestId("cal-day-2026-07-29"));
    const detail = await screen.findByTestId("calendar-day-detail");
    expect(detail).toHaveTextContent("FOMC");
    expect(detail).toHaveTextContent("tier 1");
  });
});

describe("dual-layer removal is a two-step affordance (CAL-04)", () => {
  it("shows 'Remove manual tag' first; after removing, a persisting auto-tag shows 'Suppress auto-tag (rule stays)'", async () => {
    const getCal = vi.spyOn(api, "getCalendar");
    // Initial state: BOTH layers present -- effective_tags shows the MANUAL
    // label/origin winning (slice-1 fold semantics), same as the backend.
    getCal.mockResolvedValueOnce(calendarFixture({
      tags: { "2026-07-29": { label: "FOMC (manual override)", origin: "manual", category: null } },
      standing_rules: { FOMC: null },
    }));
    const untagSpy = vi.spyOn(api, "untagCalendarDay").mockResolvedValue({ result: "untagged", day: "2026-07-29" });

    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");
    fireEvent.click(screen.getByTestId("cal-day-2026-07-29"));
    const detail = await screen.findByTestId("calendar-day-detail");
    expect(within(detail).getByRole("button", { name: /remove manual tag/i })).toBeInTheDocument();

    // After removal, the fold's own layered-removal semantics leave the
    // AUTO layer in place (slice 1) -- a re-fetch reflects that: same day,
    // now origin "auto". The UI must render exactly what comes back, never
    // assume the day is gone.
    getCal.mockResolvedValueOnce(calendarFixture({
      tags: { "2026-07-29": { label: "FOMC", origin: "auto", category: "FOMC" } },
      standing_rules: { FOMC: null },
    }));
    fireEvent.click(within(detail).getByRole("button", { name: /remove manual tag/i }));
    await waitFor(() => expect(untagSpy).toHaveBeenCalledWith("2026-07-29"));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /suppress auto-tag \(rule stays\)/i })).toBeInTheDocument());
  });

  it("a day with only an auto-tag shows 'Suppress auto-tag (rule stays)' immediately", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      tags: { "2026-09-16": { label: "FOMC", origin: "auto", category: "FOMC" } },
    }));
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    fireEvent.click(screen.getByTestId("cal-day-2026-09-16"));
    const detail = await screen.findByTestId("calendar-day-detail");
    expect(within(detail).getByRole("button", { name: /suppress auto-tag \(rule stays\)/i })).toBeInTheDocument();
    expect(within(detail).queryByRole("button", { name: /remove manual tag/i })).not.toBeInTheDocument();
  });
});

describe("standing category rules panel (CAL-04)", () => {
  it("lists every known category, checked only for an active standing rule", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({ standing_rules: { FOMC: null } }));
    render(<CalendarPage />);

    const panel = await screen.findByTestId("calendar-rules");
    expect(within(panel).getByLabelText("always block FOMC")).toBeChecked();
    expect(within(panel).getByLabelText("always block CPI")).not.toBeChecked();
  });

  it("toggling a rule ON calls setCalendarRule; effective immediately (re-fetches)", async () => {
    const getCal = vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture());
    const setRule = vi.spyOn(api, "setCalendarRule").mockResolvedValue({ result: "rule_set", category: "FOMC" });
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    fireEvent.click(screen.getByLabelText("always block FOMC"));
    await waitFor(() => expect(setRule).toHaveBeenCalledWith("FOMC"));
    await waitFor(() => expect(getCal).toHaveBeenCalledTimes(2)); // initial + re-fetch after the change
  });

  it("toggling a rule OFF calls removeCalendarRule", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({ standing_rules: { FOMC: null } }));
    const removeRule = vi.spyOn(api, "removeCalendarRule").mockResolvedValue({ result: "rule_removed", category: "FOMC" });
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    fireEvent.click(screen.getByLabelText("always block FOMC"));
    await waitFor(() => expect(removeRule).toHaveBeenCalledWith("FOMC"));
  });

  it("takes the tier label from the BACKEND payload, not the local fallback (2026-07-15 review)", async () => {
    // Backend says FOMC is tier 2 (deliberately drifted from the local
    // fallback_tier of 1): the rendered "(tier 2)" label must follow the
    // backend — the payload is the tier authority; the hardcoded list only
    // covers never-imported categories (no payload row to read from).
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture({
      staleness: {
        FOMC: { imported_at: "t", horizon: "2026-12-16", stale: false, tier: 2, dates: [] },
      },
    }));
    render(<CalendarPage />);
    const panel = await screen.findByTestId("calendar-rules");

    const fomcRow = within(panel).getByLabelText("always block FOMC").closest("label")!;
    expect(fomcRow).toHaveTextContent("Always block FOMC (tier 2)");
    // Never-imported FED_SPEAKER still shows its fallback tier-2 label.
    const speakerRow = within(panel).getByLabelText("always block FED_SPEAKER").closest("label")!;
    expect(speakerRow).toHaveTextContent("Always block FED_SPEAKER (tier 2)");
  });
});

describe("import dialog (CAL-01)", () => {
  it("posts the pasted dates for the selected category", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture());
    const importSpy = vi.spyOn(api, "importCalendarEvents").mockResolvedValue({ result: "imported", category: "FOMC", count: 2 });
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    fireEvent.click(screen.getByRole("button", { name: /import events/i }));
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByLabelText("import dates"), { target: { value: "2026-07-29\n2026-09-16" } });
    fireEvent.click(screen.getByRole("button", { name: /^import$/i }));

    await waitFor(() => expect(importSpy).toHaveBeenCalledWith(
      { category: "FOMC", dates: ["2026-07-29", "2026-09-16"] }));
  });

  it("shows a 422 error from the backend without deciding anything itself", async () => {
    vi.spyOn(api, "getCalendar").mockResolvedValue(calendarFixture());
    vi.spyOn(api, "importCalendarEvents").mockRejectedValue(new ApiError(422, { reason: "invalid_day" }));
    render(<CalendarPage />);
    await screen.findByTestId("calendar-months");

    fireEvent.click(screen.getByRole("button", { name: /import events/i }));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: /^import$/i }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
