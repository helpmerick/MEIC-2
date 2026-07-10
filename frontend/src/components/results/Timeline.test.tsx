// RPT-12: the intraday timeline renders entry/stop/close/watchdog/UNPROTECTED
// markers, and D10 forbids interpolation — a gap in the spot series must
// render as a gap (a broken path segment), never a smoothed line across it.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MarkSample, TimelineMarker } from "../../types";
import { Timeline } from "./Timeline";

function mark(at: string, spot: string): MarkSample {
  return {
    entry_id: "2026-07-10#1", at, spot,
    put_short_mid: null, put_long_mid: null, call_short_mid: null, call_long_mid: null,
  };
}

const MARKERS: TimelineMarker[] = [
  { type: "CondorFilled", icon: "▲", entry_id: "2026-07-10#1", at: "2026-07-10T09:32:00-04:00" },
  { type: "ShortStopped", icon: "✖", entry_id: "2026-07-10#1", at: "2026-07-10T10:15:00-04:00" },
];

describe("Timeline (RPT-12)", () => {
  it("renders the SPX line and every marker when samples are present", () => {
    const marks = [
      mark("2026-07-10T09:31:00-04:00", "5010.25"),
      mark("2026-07-10T09:32:00-04:00", "5011.50"),
      mark("2026-07-10T09:33:00-04:00", "5012.00"),
    ];
    render(<Timeline timeline={{ marks, markers: MARKERS }} />);
    expect(screen.getByTestId("timeline-chart")).toBeInTheDocument();
    const strip = screen.getByTestId("marker-strip");
    expect(strip).toHaveTextContent("▲");
    expect(strip).toHaveTextContent("✖");
  });

  it("breaks the path into a separate segment across a sampling gap (D10: never interpolated)", () => {
    const marks = [
      mark("2026-07-10T09:31:00-04:00", "5010.00"),
      mark("2026-07-10T09:32:00-04:00", "5011.00"),
      // a 20-minute gap — no samples in between; D10 forbids bridging it
      mark("2026-07-10T09:52:00-04:00", "5020.00"),
      mark("2026-07-10T09:53:00-04:00", "5021.00"),
    ];
    const { container } = render(<Timeline timeline={{ marks, markers: [] }} />);
    const paths = container.querySelectorAll("path.chart-line");
    // Two disconnected segments, not one continuous (interpolated) line.
    expect(paths.length).toBe(2);
    // Each segment starts fresh with "M" rather than continuing with "L".
    for (const p of paths) {
      expect(p.getAttribute("d")?.startsWith("M")).toBe(true);
    }
  });

  it("degrades to a markers-only view when no EntryMarkSample data exists (D10 fallback)", () => {
    render(<Timeline timeline={{ marks: [], markers: MARKERS }} />);
    expect(screen.getByTestId("timeline-markers-only")).toBeInTheDocument();
    expect(screen.queryByTestId("timeline-chart")).not.toBeInTheDocument();
    expect(screen.getByTestId("marker-strip")).toHaveTextContent("▲");
  });
});
