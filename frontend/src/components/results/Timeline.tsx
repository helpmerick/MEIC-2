import { useState } from "react";

import type { MarkSample, TimelineMarker } from "../../types";
import { ET_ZONE, instantToZone } from "../../time";
import { ZoomableFigure } from "../ZoomableFigure";

// RPT-12 intraday timeline: SPX line 09:30-16:00 ET with entry/stop/close/
// watchdog/UNPROTECTED markers. D10: NEVER interpolate -- a gap in the
// EntryMarkSample series renders as a broken path segment, not a smoothed
// line.
//
// Root-cause note (RPT-12 timeline rebuild, v1.77 -- operator screenshot,
// 2026-07-10 drill-down: "renders as an unreadable filled blob -- no SPX
// line, no axes, no markers"): the chart DID already draw a proper `fill:
// none` line underneath, and it DID have markers -- but every single 1-
// minute EntryMarkSample sample (D8; up to ~390 across a 6.5h day per open
// entry, more with several entries open at once) got its own small FILLED
// circle (`.chart-pt { fill: var(--accent) }`) on a chart only 640px wide.
// At under 2px of horizontal space per sample, consecutive circles overlap
// by roughly half their own diameter -- the *line* was there and correct,
// it was just buried under a solid smear of overlapping dots, with no axis
// labels to give the eye any scale reference and markers rendered as bare,
// uncoloured icon glyphs below the chart rather than shape+colour glyphs on
// it. That combination is what read as "a blob, no line, no axes, no
// markers" even though three of those four things technically existed.
//
// This rebuild: (1) real labelled x (ET time) / y (SPX price) axes: (2) the
// SPX line keeps its FULL resolution (D10 -- the line/path itself never
// loses data), but the discrete circle markers are DOWNSAMPLED FOR DISPLAY
// ONLY via per-pixel-column min/max decimation (never more than ~2 circles
// per horizontal pixel, keeping the visual peak/trough shape MAE/MFE cares
// about) -- the underlying `marks` array, and every server-computed MAE/MFE
// figure (reporting/mae_mfe.py), are completely untouched; (3) leg mid
// series (put/call short/long) render as THIN LINES on their own secondary
// (right) axis, never filled bars/areas; (4) event glyphs render ON the
// chart at their own ET time, SHAPE + COLOUR together (UI-26, never colour
// alone): entry = GREEN triangle, stop-fill = RED cross (both v1.77-ruled),
// close = circle, watchdog = lightning bolt -- each hoverable with the
// payload's own Decimal STRINGS, verbatim, never re-parsed as a float.
const WIDTH = 640;
const HEIGHT = 280;
const MARGIN = { top: 34, bottom: 34, left: 54 };
const RIGHT_MARGIN_NO_LEGS = 16;
const RIGHT_MARGIN_WITH_LEGS = 58;
const EVENT_LANE_Y = MARGIN.top - 16; // a dedicated strip above the plot for on-chart glyphs
const OPEN_MIN = 9 * 60 + 30;
const CLOSE_MIN = 16 * 60;
const SPAN_MIN = CLOSE_MIN - OPEN_MIN;
const GAP_THRESHOLD_MIN = 3; // > this many minutes between 1-min-cadence samples (D8) = a gap

/** ET wall-clock minutes-since-midnight of a journaled instant, for BOTH the
 * EntryMarkSample series and the event-glyph markers.
 *
 * DAY-03 identity fix (review, 2026-07-16 -- BLOCKING): every real writer
 * stamps `at` as a full ISO UTC instant (SystemClock.now() =
 * datetime.now(timezone.utc), traced through execute_entry / close_entry /
 * the stop-fill watch / watchdog / reconcile and EntryMarkSample's snapshot
 * `taken_at`). The previous code here regex-read the string's own hour
 * digits and CALLED them ET -- so a 14:45 ET fill (stamped 18:45Z) plotted
 * past the 16:00 close clamp and the whole afternoon collapsed onto the
 * chart's right edge. The instant must be CONVERTED to the ET wall clock via
 * the shared, DST-aware helper (time.ts instantToZone / ET_ZONE -- the same
 * conversion every other timestamp in the app uses), never substring-read.
 * The reports.py module docstring that used to license the substring read
 * ("at values are already ET") was corrected in the same review. */
function minutesOf(at: string | null): number | null {
  if (!at) return null;
  const hhmm = instantToZone(at, ET_ZONE);
  if (!hhmm) return null;
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

function fmtClock(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/** D10: break an ET-time-ordered series into disconnected segments across
 * any sampling gap wider than GAP_THRESHOLD_MIN -- a gap renders as a gap,
 * never bridged with an interpolated line, for spot OR leg series alike. */
function breakIntoSegments<T extends { min: number }>(points: T[]): T[][] {
  const segments: T[][] = [];
  let current: T[] = [];
  points.forEach((p, i) => {
    if (i > 0 && p.min - points[i - 1].min > GAP_THRESHOLD_MIN) {
      segments.push(current);
      current = [];
    }
    current.push(p);
  });
  if (current.length) segments.push(current);
  return segments;
}

interface SpotPoint {
  min: number;
  spot: number;
  spotStr: string;
  at: string;
}

function spotSeries(marks: MarkSample[]): SpotPoint[] {
  const points: SpotPoint[] = [];
  // One SPX point per TIMESTAMP (final review, 2026-07-16): the sampler
  // journals one EntryMarkSample PER OPEN ENTRY per tick, each carrying the
  // same market `spot` for that instant -- on a multi-entry day (the NORMAL
  // MEIC day) the un-deduped series held N identical points per tick, which
  // both stacked N circles on the same coordinates with duplicate React
  // keys (key={p.at}) and put duplicate vertices in the line path. Spot is
  // market data, identical across entries at a tick, so keeping the first
  // sample per `at` drops no information.
  const seen = new Set<string>();
  for (const m of marks) {
    if (m.spot == null) continue;
    if (seen.has(m.at)) continue;
    const min = minutesOf(m.at);
    if (min === null) continue;
    seen.add(m.at);
    points.push({ min, spot: Number(m.spot), spotStr: m.spot, at: m.at });
  }
  return points.sort((a, b) => a.min - b.min);
}

/** Display-only decimation (see the file-header root-cause note): keeps at
 * most the min- and max-valued sample per occupied horizontal pixel column,
 * so overlapping circles never smear into a solid blob again regardless of
 * how many 1-minute samples a long day (or several concurrent entries)
 * produces. Never touches `marks` itself, the line path, or any server-
 * computed statistic -- purely which points get their OWN drawn circle. */
function decimateForDisplay(points: SpotPoint[], xOf: (min: number) => number): SpotPoint[] {
  const buckets = new Map<number, SpotPoint[]>();
  for (const p of points) {
    const col = Math.round(xOf(p.min));
    const list = buckets.get(col);
    if (list) list.push(p);
    else buckets.set(col, [p]);
  }
  const out: SpotPoint[] = [];
  for (const list of buckets.values()) {
    if (list.length <= 2) {
      out.push(...list);
      continue;
    }
    let lo = list[0];
    let hi = list[0];
    for (const p of list) {
      if (p.spot < lo.spot) lo = p;
      if (p.spot > hi.spot) hi = p;
    }
    out.push(lo);
    if (hi !== lo) out.push(hi);
  }
  return out.sort((a, b) => a.min - b.min);
}

type LegKey = "put_short_mid" | "put_long_mid" | "call_short_mid" | "call_long_mid";

const LEG_STYLE: Record<LegKey, { color: string; dash?: string; label: string }> = {
  put_short_mid: { color: "var(--accent-2)", label: "PUT short mid" },
  put_long_mid: { color: "var(--accent-2)", dash: "4 3", label: "PUT long mid" },
  call_short_mid: { color: "var(--info)", label: "CALL short mid" },
  call_long_mid: { color: "var(--info)", dash: "4 3", label: "CALL long mid" },
};

interface LegPoint {
  min: number;
  v: number;
}

function legSeries(marks: MarkSample[], key: LegKey): LegPoint[] {
  const points: LegPoint[] = [];
  for (const m of marks) {
    const val = m[key];
    if (val == null) continue;
    const min = minutesOf(m.at);
    if (min === null) continue;
    points.push({ min, v: Number(val) });
  }
  return points.sort((a, b) => a.min - b.min);
}

// Event glyphs: shape + colour together (UI-26 -- never colour alone). Entry
// (green triangle) and stop-fill (red cross) are the v1.77-ruled pair; the
// other three pre-existing marker types keep a distinct shape too.
const ON_CHART_TYPES = new Set([
  "CondorFilled",
  "ShortStopped",
  "EntryClosed",
  "WatchdogEscalated",
]);

function Glyph({ marker, x }: { marker: TimelineMarker; x: number }) {
  const y = EVENT_LANE_Y;
  const title = `${marker.type} — ${marker.detail ?? "no detail recorded"} — ${marker.at}`;
  switch (marker.type) {
    case "CondorFilled": // entry ▲ GREEN (RPT-12/UI-26 v1.77 ruling)
      return (
        <polygon
          points={`${x},${y - 6} ${x - 6},${y + 5} ${x + 6},${y + 5}`}
          className="glyph glyph-entry"
          data-testid="glyph-entry"
        >
          <title>{title}</title>
        </polygon>
      );
    case "ShortStopped": // stop-fill ✖ RED (RPT-12/UI-26 v1.77 ruling)
      return (
        <g className="glyph glyph-stop" data-testid="glyph-stop">
          <line x1={x - 5} y1={y - 5} x2={x + 5} y2={y + 5} />
          <line x1={x - 5} y1={y + 5} x2={x + 5} y2={y - 5} />
          <title>{title}</title>
        </g>
      );
    case "EntryClosed": // close ●
      return (
        <circle cx={x} cy={y} r={4} className="glyph glyph-close" data-testid="glyph-close">
          <title>{title}</title>
        </circle>
      );
    case "WatchdogEscalated": // watchdog (lightning bolt shape)
      return (
        <polygon
          points={`${x - 2},${y - 7} ${x + 3},${y - 7} ${x - 1},${y - 1} ${x + 3},${y - 1} ${x - 3},${y + 7} ${x},${y + 1} ${x - 4},${y + 1}`}
          className="glyph glyph-watchdog"
          data-testid="glyph-watchdog"
        >
          <title>{title}</title>
        </polygon>
      );
    default:
      return null;
  }
}

function MarkerStrip({ markers, heading }: { markers: TimelineMarker[]; heading?: string }) {
  if (markers.length === 0) return null;
  return (
    <div>
      {heading && <p className="chart-legend-note">{heading}</p>}
      <div className="marker-strip" data-testid="marker-strip">
        {markers.map((m, i) => (
          <span
            key={i}
            className={`marker marker-${m.type}`}
            title={`${m.type} — ${m.detail ?? m.entry_id ?? "—"} — ${m.at ?? "no timestamp recorded"}`}
          >
            {m.icon}
          </span>
        ))}
      </div>
    </div>
  );
}

function TimelineChart({
  timeline,
}: {
  timeline: { marks: MarkSample[]; markers: TimelineMarker[] };
}) {
  // IMPLEMENTATION DECISION (final review, 2026-07-16 -- FLAGGED for
  // operator reversal, C-flag style; the ratified RPT-12 text is silent on
  // multi-entry leg rendering): the sampler journals leg mids PER ENTRY, and
  // a MEIC day normally has SEVERAL entries open at once -- rendering every
  // entry's four leg series as one merged line produced a full-axis-height
  // sawtooth (each timestamp carried N different entries' values in
  // sequence). Ruling applied here:
  //   - whole-day ("All") view on a multi-entry day renders SPX + event
  //     glyphs ONLY, no leg lines;
  //   - an entry selector (pills) scopes the chart's LEG series to one
  //     entry's own samples, at which point its four leg-mid lines render
  //     honestly for that one condor;
  //   - a day with exactly ONE sampled entry renders its legs by default,
  //     no selector shown.
  // The SPX line and the event glyphs always stay whole-day: spot is market
  // data shared by every entry, and the glyphs' own hover carries entry ids.
  const [selectedEntry, setSelectedEntry] = useState<string>("ALL");

  const points = spotSeries(timeline.marks);

  // D10 markers-only fallback: no EntryMarkSample data this day -- no chart
  // (and therefore no axes) can honestly be drawn, so every marker (even one
  // with a plottable `at`) renders in the flat strip, never a fabricated
  // chart with no underlying spot data.
  if (points.length === 0) {
    return (
      <div data-testid="timeline-markers-only">
        <p className="muted">
          No EntryMarkSample data this day — degraded to markers-only (D10 fallback: never
          interpolated).
        </p>
        <MarkerStrip markers={timeline.markers} />
      </div>
    );
  }

  // Markers that carry a valid ET timestamp WITHIN the 09:30-16:00 plot
  // range render ON the chart, at their own x position. Everything else
  // renders in the below-chart strip with its true recorded time, matching
  // the component's null-`at` stance (final review, 2026-07-16): a marker
  // with no `at` (SideUnprotected has no such field; a pre-v1.67 replayed
  // log can leave the others null too) has no honest x position, and an
  // OUT-OF-HOURS instant (e.g. an EOD sweep after 16:00 ET) must not be
  // silently clamped onto the plot edge -- that draws a visually false time.
  const onChartMarkers: { marker: TimelineMarker; min: number }[] = [];
  const unplottableMarkers: TimelineMarker[] = [];
  for (const m of timeline.markers) {
    const min = ON_CHART_TYPES.has(m.type) ? minutesOf(m.at) : null;
    if (min !== null && min >= OPEN_MIN && min <= CLOSE_MIN) {
      onChartMarkers.push({ marker: m, min });
    } else {
      unplottableMarkers.push(m);
    }
  }

  // Entries that actually have samples this day, in first-seen order.
  const entryIds = [...new Set(timeline.marks.map((m) => m.entry_id))];
  // With a single sampled entry the selector is pointless -- legs render for
  // it directly. A stale selection (entry not in this day's data) falls back
  // to the whole-day view rather than an empty chart.
  const effectiveEntry =
    entryIds.length === 1
      ? entryIds[0]
      : entryIds.includes(selectedEntry)
        ? selectedEntry
        : "ALL";
  const legMarks =
    effectiveEntry === "ALL" ? [] : timeline.marks.filter((m) => m.entry_id === effectiveEntry);

  const legKeys = Object.keys(LEG_STYLE) as LegKey[];
  const legSeriesByKey = new Map(legKeys.map((k) => [k, legSeries(legMarks, k)]));
  const hasLegs = legKeys.some((k) => (legSeriesByKey.get(k)?.length ?? 0) > 0);

  const rightMargin = hasLegs ? RIGHT_MARGIN_WITH_LEGS : RIGHT_MARGIN_NO_LEGS;
  const plotLeft = MARGIN.left;
  const plotRight = WIDTH - rightMargin;
  const plotTop = MARGIN.top;
  const plotBottom = HEIGHT - MARGIN.bottom;

  const xOf = (min: number): number => {
    const clamped = Math.min(CLOSE_MIN, Math.max(OPEN_MIN, min));
    return plotLeft + ((clamped - OPEN_MIN) / SPAN_MIN) * (plotRight - plotLeft);
  };

  const values = points.map((p) => p.spot);
  const spotMin = Math.min(...values);
  const spotMax = Math.max(...values);
  const spotSpan = spotMax - spotMin || 1;
  const yOf = (v: number) => plotBottom - ((v - spotMin) / spotSpan) * (plotBottom - plotTop);

  const spotSegments = breakIntoSegments(points);
  const decimated = decimateForDisplay(points, xOf);

  // Secondary (right) axis for the leg mid series -- SPX (thousands) and
  // option mids (single digits) don't share a scale; plotting both against
  // the SPX axis would flatten the legs into an invisible line at the
  // bottom, which would just trade one form of illegibility for another.
  let legMin = Infinity;
  let legMax = -Infinity;
  if (hasLegs) {
    for (const key of legKeys) {
      for (const p of legSeriesByKey.get(key) ?? []) {
        if (p.v < legMin) legMin = p.v;
        if (p.v > legMax) legMax = p.v;
      }
    }
  }
  const legSpan = legMax - legMin || 1;
  const legYOf = (v: number) => plotBottom - ((v - legMin) / legSpan) * (plotBottom - plotTop);

  // X axis: hourly ET ticks from open through the 16:00 close.
  const xTicks: number[] = [];
  for (let m = OPEN_MIN; m < CLOSE_MIN; m += 60) xTicks.push(m);
  xTicks.push(CLOSE_MIN);

  // Y axis (SPX, left): four evenly spaced ticks across the day's own range.
  const yTicks = [0, 1, 2, 3].map((i) => spotMin + (spotSpan * i) / 3);

  return (
    <div className="chart-wrap">
      {/* Entry selector -- multi-entry days only (see the implementation-
          decision note at the top of this component). Lives OUTSIDE the
          ZoomableFigure so one selection drives both the inline chart and
          the full-screen overlay copy. */}
      {entryIds.length > 1 && (
        <div className="timeline-entry-selector" data-testid="timeline-entry-selector">
          <span className="chart-legend-note">Leg mids per entry:</span>
          <button
            type="button"
            className={`entry-pill ${effectiveEntry === "ALL" ? "active" : ""}`}
            aria-pressed={effectiveEntry === "ALL"}
            onClick={() => setSelectedEntry("ALL")}
          >
            All (SPX + events only)
          </button>
          {entryIds.map((id) => (
            <button
              key={id}
              type="button"
              className={`entry-pill ${effectiveEntry === id ? "active" : ""}`}
              aria-pressed={effectiveEntry === id}
              onClick={() => setSelectedEntry(id)}
            >
              {id}
            </button>
          ))}
        </div>
      )}
      <ZoomableFigure label="Intraday SPX timeline">
        {() => (
          <div data-testid="timeline-chart">
            <svg
              viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
              className="timeline-chart"
              role="img"
              aria-label="Intraday SPX timeline with labeled axes, entry/stop/close/watchdog markers, and leg mid lines"
            >
              {/* Axes */}
              <line x1={plotLeft} x2={plotRight} y1={plotBottom} y2={plotBottom} className="chart-axis" />
              <line x1={plotLeft} x2={plotLeft} y1={plotTop} y2={plotBottom} className="chart-axis" />
              {xTicks.map((m) => (
                <g key={m}>
                  <line x1={xOf(m)} x2={xOf(m)} y1={plotBottom} y2={plotBottom + 4} className="chart-axis" />
                  <text x={xOf(m)} y={plotBottom + 14} className="chart-axis-label" textAnchor="middle">
                    {fmtClock(m)}
                  </text>
                </g>
              ))}
              {yTicks.map((v, i) => (
                <g key={i}>
                  <line x1={plotLeft - 4} x2={plotLeft} y1={yOf(v)} y2={yOf(v)} className="chart-axis" />
                  <text x={plotLeft - 7} y={yOf(v) + 3} className="chart-axis-label" textAnchor="end">
                    {v.toFixed(2)}
                  </text>
                </g>
              ))}
              {hasLegs && (
                <>
                  <line x1={plotRight} x2={plotRight} y1={plotTop} y2={plotBottom} className="chart-axis" />
                  {[0, 1, 2].map((i) => {
                    const v = legMin + (legSpan * i) / 2;
                    return (
                      <g key={i}>
                        <line x1={plotRight} x2={plotRight + 4} y1={legYOf(v)} y2={legYOf(v)} className="chart-axis" />
                        <text x={plotRight + 7} y={legYOf(v) + 3} className="chart-axis-label" textAnchor="start">
                          {v.toFixed(2)}
                        </text>
                      </g>
                    );
                  })}
                </>
              )}

              {/* Leg mid lines: thin, unfilled -- never a filled bar/area. */}
              {hasLegs &&
                legKeys.map((key) => {
                  const style = LEG_STYLE[key];
                  const segs = breakIntoSegments(legSeriesByKey.get(key) ?? []);
                  return segs.map((seg, si) => (
                    <path
                      key={`${key}-${si}`}
                      d={seg.map((p, i) => `${i === 0 ? "M" : "L"} ${xOf(p.min)} ${legYOf(p.v)}`).join(" ")}
                      className="chart-leg-line"
                      style={{ stroke: style.color, strokeDasharray: style.dash }}
                      data-testid={`leg-line-${key}`}
                    >
                      <title>{style.label}</title>
                    </path>
                  ));
                })}

              {/* SPX line -- FULL resolution, never decimated (D10: the line
                  itself carries every sample; only the discrete circle
                  markers below are downsampled for display). */}
              {spotSegments.map((seg, si) => (
                <path
                  key={si}
                  d={seg.map((p, i) => `${i === 0 ? "M" : "L"} ${xOf(p.min)} ${yOf(p.spot)}`).join(" ")}
                  className="chart-line"
                />
              ))}
              {decimated.map((p) => (
                <circle key={p.at} cx={xOf(p.min)} cy={yOf(p.spot)} r={1.5} className="chart-pt">
                  <title>{`${p.at} — SPX ${p.spotStr}`}</title>
                </circle>
              ))}

              {/* Event glyphs -- shape + colour together (UI-26). */}
              {onChartMarkers.map(({ marker, min }, i) => (
                <Glyph key={i} marker={marker} x={xOf(min)} />
              ))}
            </svg>
          </div>
        )}
      </ZoomableFigure>
      <p className="chart-legend-note">
        ▲ entry (green) · ✖ stop-fill (red) · ● close · watchdog (bolt)
        {hasLegs && " · thin lines: leg mids (put purple, call blue; solid = short, dashed = long)"}
      </p>
      <MarkerStrip
        markers={unplottableMarkers}
        heading={
          unplottableMarkers.length > 0
            ? "Events that cannot be placed on the 09:30–16:00 chart (no recorded time, or outside market hours — true time in the hover):"
            : undefined
        }
      />
    </div>
  );
}

export function Timeline({
  timeline,
}: {
  timeline: { marks: MarkSample[]; markers: TimelineMarker[] };
}) {
  return <TimelineChart timeline={timeline} />;
}
