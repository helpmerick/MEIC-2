import type { ContractBreach, DailyRow } from "../../types";
import { dollars } from "./shared";

// RPT-09/RPT-14 equity curve with drawdown shading. Hand-rolled SVG (no chart
// library installed, per the slice-3 brief) — CSS-var themed so it inverts
// cleanly with the app's light/dark toggle.
const WIDTH = 640;
const HEIGHT = 220;
const PAD = 28;

interface Point {
  date: string;
  netStr: string;
  cum: number;
  peak: number;
}

// The running total is PRESENTATION math for the curve's Y position only
// (UI-26 explicitly allows "positions for bar/point layout"); every hover
// title still shows the day's own server Decimal string, never the derived
// running total, as the authoritative number.
function buildPoints(daily: DailyRow[]): Point[] {
  let cum = 0;
  let peak = -Infinity;
  return daily.map((d) => {
    cum += Number(d.net_pnl);
    peak = Math.max(peak, cum);
    return { date: d.date, netStr: d.net_pnl, cum, peak };
  });
}

// RPT-03 contract-audit breaches carry their own entry_id ("{day}#{n}") — the
// day is a straight string-prefix read, not a derived value.
function breachDaySet(breaches: ContractBreach[]): Set<string> {
  return new Set(breaches.map((b) => b.entry_id.split("#", 1)[0]));
}

export function EquityCurve({ daily, breaches }: { daily: DailyRow[]; breaches: ContractBreach[] }) {
  if (daily.length === 0) {
    return <p className="muted" data-testid="equity-empty">No trading days in this period yet.</p>;
  }

  const points = buildPoints(daily);
  const values = points.flatMap((p) => [p.cum, p.peak]);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min || 1;
  const xStep = points.length > 1 ? (WIDTH - 2 * PAD) / (points.length - 1) : 0;
  const x = (i: number) => PAD + i * xStep;
  const y = (v: number) => HEIGHT - PAD - ((v - min) / span) * (HEIGHT - 2 * PAD);

  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"} ${x(i)} ${y(p.cum)}`).join(" ");
  const areaPath =
    `${linePath} L ${x(points.length - 1)} ${y(points[points.length - 1].peak)} ` +
    points
      .slice()
      .reverse()
      .map((p, ri) => `L ${x(points.length - 1 - ri)} ${y(p.peak)}`)
      .join(" ") +
    " Z";
  const peakPath = points.map((p, i) => `${i === 0 ? "M" : "L"} ${x(i)} ${y(p.peak)}`).join(" ");

  const breachDays = breachDaySet(breaches);
  const zeroY = y(0);

  return (
    <div className="chart-wrap" data-testid="equity-curve">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="equity-chart"
        role="img"
        aria-label="Equity curve with drawdown shading"
      >
        <line x1={PAD} x2={WIDTH - PAD} y1={zeroY} y2={zeroY} className="chart-zero" />
        <path d={areaPath} className="chart-drawdown" />
        <path d={peakPath} className="chart-peak" />
        <path d={linePath} className="chart-line" />
        {points.map((p, i) => {
          const breach = breachDays.has(p.date);
          return (
            <circle
              key={p.date}
              cx={x(i)}
              cy={y(p.cum)}
              r={breach ? 4 : 2.5}
              className={`chart-pt ${breach ? "breach" : ""}`}
            >
              <title>
                {`${p.date}: ${dollars(p.netStr)} that day (running total $${p.cum.toFixed(2)})`}
                {breach ? " — contract-audit breach (RPT-03)" : ""}
              </title>
            </circle>
          );
        })}
      </svg>
      {breachDays.size > 0 && (
        <p className="chart-legend-note" data-testid="equity-breach-note">
          ⚑ {breachDays.size} day(s) with a contract-audit breach (RPT-03) marked above.
        </p>
      )}
      <p className="chart-legend-note gap-note">
        Watchdog/UNPROTECTED equity-curve markers (RPT-14) need per-event day attribution the API
        doesn't expose yet — see the Health band for their aggregate counts instead.
      </p>
    </div>
  );
}
