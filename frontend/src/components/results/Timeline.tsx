import type { MarkSample, TimelineMarker } from "../../types";

// RPT-12 intraday timeline: SPX line 09:30-16:00 ET with entry/stop/close/
// watchdog/UNPROTECTED markers. D10: NEVER interpolate — a gap in the
// EntryMarkSample series renders as a broken path segment, not a smoothed line.
const WIDTH = 640;
const HEIGHT = 200;
const PAD = 28;
const OPEN_MIN = 9 * 60 + 30;
const CLOSE_MIN = 16 * 60;
const SPAN_MIN = CLOSE_MIN - OPEN_MIN;
const GAP_THRESHOLD_MIN = 3; // > this many minutes between 1-min-cadence samples (D8) = a gap

function minutesOf(at: string | null): number | null {
  if (!at) return null;
  // The bot's own `at` values are already ET (doc 10 module note) — no
  // timezone conversion needed, just read the wall-clock hour:minute.
  const m = /T(\d{2}):(\d{2})/.exec(at);
  if (!m) return null;
  return +m[1] * 60 + +m[2];
}

function xOf(min: number): number {
  const clamped = Math.min(CLOSE_MIN, Math.max(OPEN_MIN, min));
  return PAD + ((clamped - OPEN_MIN) / SPAN_MIN) * (WIDTH - 2 * PAD);
}

interface SpotPoint {
  min: number;
  spot: number;
  spotStr: string;
  at: string;
}

function spotSeries(marks: MarkSample[]): SpotPoint[] {
  const points: SpotPoint[] = [];
  for (const m of marks) {
    if (m.spot == null) continue;
    const min = minutesOf(m.at);
    if (min === null) continue;
    points.push({ min, spot: Number(m.spot), spotStr: m.spot, at: m.at });
  }
  return points.sort((a, b) => a.min - b.min);
}

function MarkerStrip({ markers }: { markers: TimelineMarker[] }) {
  if (markers.length === 0) return <p className="muted">No markers this day.</p>;
  return (
    <div className="marker-strip" data-testid="marker-strip">
      {markers.map((m, i) => (
        <span
          key={i}
          className={`marker marker-${m.type}`}
          title={`${m.type} — ${m.entry_id ?? "—"} — ${m.at ?? "no timestamp recorded"}`}
        >
          {m.icon}
        </span>
      ))}
    </div>
  );
}

export function Timeline({
  timeline,
}: {
  timeline: { marks: MarkSample[]; markers: TimelineMarker[] };
}) {
  const points = spotSeries(timeline.marks);

  // D10 markers-only fallback: no EntryMarkSample data this day.
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

  const values = points.map((p) => p.spot);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const y = (v: number) => HEIGHT - PAD - ((v - min) / span) * (HEIGHT - 2 * PAD);

  // Break into disconnected segments across any gap > GAP_THRESHOLD_MIN — a
  // gap renders as a gap, never smoothed over with an interpolated line.
  const segments: SpotPoint[][] = [];
  let current: SpotPoint[] = [];
  points.forEach((p, i) => {
    if (i > 0 && p.min - points[i - 1].min > GAP_THRESHOLD_MIN) {
      segments.push(current);
      current = [];
    }
    current.push(p);
  });
  if (current.length) segments.push(current);

  return (
    <div className="chart-wrap" data-testid="timeline-chart">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="timeline-chart"
        role="img"
        aria-label="Intraday SPX timeline with entry/stop/close markers"
      >
        {segments.map((seg, si) => (
          <path
            key={si}
            d={seg.map((p, i) => `${i === 0 ? "M" : "L"} ${xOf(p.min)} ${y(p.spot)}`).join(" ")}
            className="chart-line"
          />
        ))}
        {points.map((p) => (
          <circle key={p.at} cx={xOf(p.min)} cy={y(p.spot)} r={1.5} className="chart-pt">
            <title>{`${p.at} — SPX ${p.spotStr}`}</title>
          </circle>
        ))}
      </svg>
      <MarkerStrip markers={timeline.markers} />
    </div>
  );
}
