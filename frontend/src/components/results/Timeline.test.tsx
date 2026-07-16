// RPT-12: the intraday timeline renders a labelled-axis SPX line, thin leg
// mid lines, and shape+colour event glyphs -- D10 forbids interpolation (a
// gap in the spot series must render as a gap, never smoothed over), and the
// v1.77 rebuild forbids the "filled blob" regression (see Timeline.tsx's own
// header for the root-cause note this suite pins against).
//
// Fixture honesty (review, 2026-07-16 -- BLOCKING finding): every real
// writer stamps `at` as a full ISO UTC instant (SystemClock.now() =
// datetime.now(timezone.utc); EntryMarkSample's snapshot `taken_at` too), so
// every fixture here is UTC-stamped exactly like a real payload. July is EDT
// (UTC-4): "13:31Z" below IS 09:31 ET. The suite's previous fixtures were
// pre-shaped with a -04:00 offset, which HID the component's UTC/ET
// conflation (regex-reading the string's own hour digits and calling them
// ET); against these UTC stamps that approach fails loudly -- a 13:31Z
// sample would plot at "13:31 ET" instead of 09:31 ET.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { MarkSample, TimelineMarker } from "../../types";
import { Timeline } from "./Timeline";

function mark(at: string, spot: string | null, legs: Partial<MarkSample> = {}): MarkSample {
  return {
    entry_id: "2026-07-10#1",
    at,
    spot,
    put_short_mid: null,
    put_long_mid: null,
    call_short_mid: null,
    call_long_mid: null,
    ...legs,
  };
}

const ENTRY_MARKER: TimelineMarker = {
  type: "CondorFilled",
  icon: "▲",
  entry_id: "2026-07-10#1",
  at: "2026-07-10T13:32:00+00:00", // 09:32 ET (EDT season)
  detail: "net credit 4.125",
};
const STOP_MARKER: TimelineMarker = {
  type: "ShortStopped",
  icon: "✖",
  entry_id: "2026-07-10#1",
  at: "2026-07-10T14:15:00+00:00", // 10:15 ET (EDT season)
  detail: "PUT fill 3.905, slippage 0.105",
};
const UNPLOTTABLE_MARKER: TimelineMarker = {
  type: "SideUnprotected",
  icon: "▓",
  entry_id: "2026-07-10#1",
  at: null,
  detail: "PUT unprotected -- flatten_side",
};

describe("Timeline (RPT-12)", () => {
  it("renders the SPX line and labelled x/y axes when samples are present", () => {
    const marks = [
      mark("2026-07-10T13:31:00+00:00", "5010.25"), // 09:31 ET
      mark("2026-07-10T13:32:00+00:00", "5011.50"),
      mark("2026-07-10T13:33:00+00:00", "5012.00"),
    ];
    const { container } = render(<Timeline timeline={{ marks, markers: [] }} />);
    expect(screen.getByTestId("timeline-chart")).toBeInTheDocument();
    expect(container.querySelector("path.chart-line")).toBeInTheDocument();
    // Labelled axes: real tick text elements, not just reserved blank margin.
    const axisLabels = container.querySelectorAll("text.chart-axis-label");
    expect(axisLabels.length).toBeGreaterThan(0);
    expect(Array.from(axisLabels).some((t) => /\d{2}:\d{2}/.test(t.textContent ?? ""))).toBe(true);
  });

  it("plots a UTC-stamped instant at its ET wall-clock position (DAY-03 -- the shared time helper, never a substring read)", () => {
    // 13:31Z = 09:31 ET = one minute after the open. The point must sit hard
    // against the LEFT edge of the plot. The pre-fix regex read ("13:31")
    // would put it 4 hours later -- past mid-chart -- which is exactly how
    // the afternoon collapsed onto the right edge in the operator's
    // screenshot once real (UTC-stamped) data flowed.
    const marks = [mark("2026-07-10T13:31:00+00:00", "5010.00")];
    const { container } = render(<Timeline timeline={{ marks, markers: [] }} />);
    const cx = Number(container.querySelector("circle.chart-pt")?.getAttribute("cx"));
    expect(cx).toBeGreaterThan(0);
    expect(cx).toBeLessThan(80); // left edge of a 640-wide chart, not mid-chart (~410 for the regex read)
  });

  it("converts through the DST-aware helper: 09:31 ET plots at the SAME x in EDT and EST seasons", () => {
    // Summer (EDT, UTC-4): 13:31Z = 09:31 ET. Winter (EST, UTC-5): 14:31Z =
    // 09:31 ET. Same ET wall-clock, different UTC hour -- any fixed-offset
    // (or regex) shortcut plots these two an hour apart.
    const summer = render(
      <Timeline timeline={{ marks: [mark("2026-07-10T13:31:00+00:00", "5010.00")], markers: [] }} />,
    );
    const winter = render(
      <Timeline timeline={{ marks: [mark("2026-01-15T14:31:00+00:00", "5010.00")], markers: [] }} />,
    );
    const cxOf = (r: { container: HTMLElement }) =>
      r.container.querySelector("circle.chart-pt")?.getAttribute("cx");
    expect(cxOf(summer)).toBeDefined();
    expect(cxOf(summer)).toBe(cxOf(winter));
  });

  it("renders the entry marker as an on-chart GREEN triangle, never in the below-chart strip", () => {
    const marks = [mark("2026-07-10T13:31:00+00:00", "5010.00")];
    const { container } = render(
      <Timeline timeline={{ marks, markers: [ENTRY_MARKER] }} />,
    );
    const glyph = screen.getByTestId("glyph-entry");
    expect(glyph.tagName.toLowerCase()).toBe("polygon"); // shape, not just a coloured dot
    expect(glyph).toHaveClass("glyph-entry");
    expect(within(glyph).getByText(/net credit 4\.125/)).toBeInTheDocument(); // exact Decimal string, verbatim
    // shape + colour together (UI-26): the CSS class carries the colour, the
    // <polygon> shape carries the "never colour alone" half of that rule.
    expect(container.querySelector('[data-testid="marker-strip"]')).not.toBeInTheDocument();
  });

  it("renders the stop-fill marker as an on-chart RED cross, hoverable with the exact fill/slippage", () => {
    const marks = [mark("2026-07-10T14:14:00+00:00", "5010.00")]; // 10:14 ET
    render(<Timeline timeline={{ marks, markers: [STOP_MARKER] }} />);
    const glyph = screen.getByTestId("glyph-stop");
    expect(glyph.querySelectorAll("line")).toHaveLength(2); // a cross, drawn as two crossing lines
    expect(glyph).toHaveClass("glyph-stop");
    expect(within(glyph).getByText(/PUT fill 3\.905, slippage 0\.105/)).toBeInTheDocument();
  });

  it("a marker with no recorded time renders in the below-chart strip, never a fabricated position", () => {
    const marks = [mark("2026-07-10T13:31:00+00:00", "5010.00")];
    render(<Timeline timeline={{ marks, markers: [UNPLOTTABLE_MARKER] }} />);
    const strip = screen.getByTestId("marker-strip");
    expect(strip).toHaveTextContent("▓");
    expect(screen.getByText(/cannot be placed on the 09:30–16:00 chart/i)).toBeInTheDocument();
  });

  it("breaks the SPX path into a separate segment across a sampling gap (D10: never interpolated)", () => {
    const marks = [
      mark("2026-07-10T13:31:00+00:00", "5010.00"), // 09:31 ET
      mark("2026-07-10T13:32:00+00:00", "5011.00"),
      // a 20-minute gap -- no samples in between; D10 forbids bridging it
      mark("2026-07-10T13:52:00+00:00", "5020.00"), // 09:52 ET
      mark("2026-07-10T13:53:00+00:00", "5021.00"),
    ];
    const { container } = render(<Timeline timeline={{ marks, markers: [] }} />);
    const paths = container.querySelectorAll("path.chart-line");
    expect(paths.length).toBe(2);
    for (const p of paths) {
      expect(p.getAttribute("d")?.startsWith("M")).toBe(true);
    }
  });

  it("degrades to a markers-only view when no EntryMarkSample data exists (D10 fallback)", () => {
    render(<Timeline timeline={{ marks: [], markers: [ENTRY_MARKER, STOP_MARKER] }} />);
    expect(screen.getByTestId("timeline-markers-only")).toBeInTheDocument();
    expect(screen.queryByTestId("timeline-chart")).not.toBeInTheDocument();
    expect(screen.getByTestId("marker-strip")).toHaveTextContent("▲");
  });

  it("renders leg mid series as thin unfilled lines on their own axis, never a filled bar", () => {
    // Single-entry day (every mark is entry #1): legs render BY DEFAULT with
    // no selector shown (final review 2026-07-16 -- the multi-entry
    // behaviour is pinned separately below).
    const marks = [
      mark("2026-07-10T13:31:00+00:00", "5010.00", { put_short_mid: "2.10", call_short_mid: "1.90" }),
      mark("2026-07-10T13:32:00+00:00", "5011.00", { put_short_mid: "2.20", call_short_mid: "1.95" }),
    ];
    const { container } = render(<Timeline timeline={{ marks, markers: [] }} />);
    expect(screen.queryByTestId("timeline-entry-selector")).not.toBeInTheDocument();
    const putLine = container.querySelector('[data-testid="leg-line-put_short_mid"]');
    const callLine = container.querySelector('[data-testid="leg-line-call_short_mid"]');
    expect(putLine).toBeInTheDocument();
    expect(callLine).toBeInTheDocument();
    expect(putLine?.tagName.toLowerCase()).toBe("path");
    // Never a filled bar/area: the shared .chart-leg-line rule is fill:none,
    // and nothing here overrides fill on individual leg paths.
    expect(putLine).toHaveClass("chart-leg-line");
    expect(putLine).not.toHaveAttribute("fill", "var(--accent-2)");
    // A secondary axis renders because leg data is present this day.
    expect(container.querySelectorAll("text.chart-axis-label").length).toBeGreaterThan(4);
  });

  it("omits leg lines and the secondary axis entirely when no leg data exists this day", () => {
    const marks = [mark("2026-07-10T13:31:00+00:00", "5010.00")];
    const { container } = render(<Timeline timeline={{ marks, markers: [] }} />);
    expect(container.querySelectorAll('[class*="leg-line"]').length).toBe(0);
  });

  // ---- multi-entry day (final review, 2026-07-16 -- flagged implementation
  // decision, see Timeline.tsx). The sampler journals one EntryMarkSample
  // PER OPEN ENTRY per tick; two entries with different leg mids at the same
  // timestamps is the NORMAL MEIC day, and the pre-fix merged leg series
  // rendered it as a full-axis-height sawtooth.
  const twoEntryMarks = [
    mark("2026-07-10T13:31:00+00:00", "5010.00", { put_short_mid: "2.10" }),
    mark("2026-07-10T13:31:00+00:00", "5010.00", { entry_id: "2026-07-10#2", put_short_mid: "4.60" }),
    mark("2026-07-10T13:32:00+00:00", "5011.00", { put_short_mid: "2.20" }),
    mark("2026-07-10T13:32:00+00:00", "5011.00", { entry_id: "2026-07-10#2", put_short_mid: "4.70" }),
  ];

  it("a multi-entry day defaults to 'All': SPX line + glyphs render, leg lines do NOT (no merged sawtooth)", () => {
    const { container } = render(
      <Timeline timeline={{ marks: twoEntryMarks, markers: [ENTRY_MARKER] }} />,
    );
    expect(container.querySelectorAll('[data-testid^="leg-line"]').length).toBe(0);
    expect(container.querySelector("path.chart-line")).toBeInTheDocument();
    expect(screen.getByTestId("glyph-entry")).toBeInTheDocument();
    expect(screen.getByTestId("timeline-entry-selector")).toBeInTheDocument();
  });

  it("selecting one entry scopes the leg lines to that entry's own samples (sane vertices, no sawtooth)", async () => {
    const { container } = render(<Timeline timeline={{ marks: twoEntryMarks, markers: [] }} />);
    await userEvent.click(screen.getByRole("button", { name: "2026-07-10#1" }));
    const put = container.querySelector('[data-testid="leg-line-put_short_mid"]');
    expect(put).toBeInTheDocument();
    // Entry #1 has exactly 2 samples => exactly 2 path vertices (M + L). The
    // pre-fix merged series would carry all 4 (both entries interleaved at
    // each timestamp => the sawtooth this pin forbids).
    expect(put?.getAttribute("d")?.split(/[ML]/).filter(Boolean).length).toBe(2);
    // Switching to the other entry swaps the scoped series, same shape.
    await userEvent.click(screen.getByRole("button", { name: "2026-07-10#2" }));
    const put2 = container.querySelector('[data-testid="leg-line-put_short_mid"]');
    expect(put2?.getAttribute("d")?.split(/[ML]/).filter(Boolean).length).toBe(2);
  });

  it("dedupes the SPX series to one point per timestamp on a multi-entry day (no duplicate React keys)", () => {
    // Four samples across two entries share two timestamps; spot is market
    // data (identical across entries at a tick) so exactly two circles --
    // and therefore two unique key={p.at} values -- must render.
    const { container } = render(<Timeline timeline={{ marks: twoEntryMarks, markers: [] }} />);
    expect(container.querySelectorAll("circle.chart-pt").length).toBe(2);
  });

  it("an after-16:00 ET instant renders in the below-chart strip with its true time, never clamped onto the plot", () => {
    const eodMarker: TimelineMarker = {
      type: "EntryClosed",
      icon: "●",
      entry_id: "2026-07-10#1",
      at: "2026-07-10T20:30:00+00:00", // 16:30 ET (EDT) -- after the close
      detail: "initiator eod",
    };
    const marks = [mark("2026-07-10T13:31:00+00:00", "5010.00")];
    render(<Timeline timeline={{ marks, markers: [eodMarker] }} />);
    // Not clamped onto the plot edge as a visually false 16:00 position...
    expect(screen.queryByTestId("glyph-close")).not.toBeInTheDocument();
    // ...but in the honest strip, true recorded instant in the hover.
    const strip = screen.getByTestId("marker-strip");
    expect(strip).toHaveTextContent("●");
    expect(within(strip).getByTitle(/2026-07-10T20:30:00\+00:00/)).toBeInTheDocument();
  });

  it("downsamples the DISPLAY circles for a dense sample run without dropping any line data", () => {
    // A burst of 60 one-per-second samples inside a single ET minute -- all
    // 60 map to (much less than) one horizontal pixel column on a 640px
    // chart. Pre-rebuild, this is exactly the "every sample gets its own
    // filled circle" pattern that produced the reported blob.
    const marks = Array.from({ length: 60 }, (_, i) => {
      const ss = String(i).padStart(2, "0");
      // Oscillate the value so min/max decimation has real peaks/troughs to
      // keep, not a flat run.
      const spot = 5000 + (i % 2 === 0 ? i * 0.01 : -i * 0.01);
      return mark(`2026-07-10T13:31:${ss}+00:00`, spot.toFixed(2)); // 09:31:ss ET
    });
    const { container } = render(<Timeline timeline={{ marks, markers: [] }} />);
    const path = container.querySelector("path.chart-line")!;
    // The line itself keeps EVERY sample (D10: never decimate the data) --
    // one M + 59 L commands.
    expect(path.getAttribute("d")?.split(/[ML]/).filter(Boolean).length).toBe(60);
    // The DISPLAY circles are decimated: far fewer than 60 for a burst that
    // dense, and never so many that neighbouring circles necessarily
    // overlap into a solid smear again.
    const circles = container.querySelectorAll("circle.chart-pt");
    expect(circles.length).toBeLessThan(10);
    expect(circles.length).toBeGreaterThan(0);
  });

  it("is wrapped in the shared zoomable figure (click opens the full-screen pan/zoom view)", async () => {
    const marks = [mark("2026-07-10T13:31:00+00:00", "5010.00")];
    render(<Timeline timeline={{ marks, markers: [] }} />);
    await userEvent.click(screen.getByTestId("zoomable-trigger"));
    expect(await screen.findByTestId("zoom-overlay")).toBeInTheDocument();
  });

  it("hover on an SPX point shows the payload's own Decimal string, never a re-parsed float", () => {
    const marks = [mark("2026-07-10T13:31:00+00:00", "5010.250")];
    const { container } = render(<Timeline timeline={{ marks, markers: [] }} />);
    const pt = container.querySelector("circle.chart-pt");
    expect(pt?.querySelector("title")?.textContent).toContain("5010.250");
  });
});
