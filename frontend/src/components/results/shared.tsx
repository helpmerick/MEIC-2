import { api } from "../../api";
import type { ReportPeriod } from "../../api";
import type { TrustBlock } from "../../types";

// --- formatting helpers (mirrors DayReportView/EntryCards/SchedulePanel's
// existing `money`/`Number(...)` conventions — Decimal strings in, 2dp
// display out; the RAW string always still reaches the DOM via title="" for
// an exact hover value, UI-26's "hover = exact Decimal values" rule). --------

export function money(v: string | null | undefined): string {
  if (v == null) return "—";
  const n = Number(v);
  return (n >= 0 ? "+" : "") + n.toFixed(2);
}

export function dollars(v: string | null | undefined): string {
  if (v == null) return "—";
  const n = Number(v);
  return (n >= 0 ? "+$" : "-$") + Math.abs(n).toFixed(2);
}

/** A non-negative money amount (fees, credits) — no forced sign. */
export function plainDollars(v: string | null | undefined): string {
  if (v == null) return "—";
  return `$${Number(v).toFixed(2)}`;
}

/** An exact Decimal FRACTION (e.g. "0.8") rendered as a percentage. Presentation
 * math only (RPT-04 already returns the exact fraction; no re-derivation). */
export function pct(v: string | null | undefined, dp = 1): string {
  if (v == null) return "—";
  return `${(Number(v) * 100).toFixed(dp)}%`;
}

export function signClass(v: string | null | undefined): string {
  if (v == null) return "";
  return Number(v) >= 0 ? "pos" : "neg";
}

// --- UI-25 trust badge -------------------------------------------------------

export function TrustBadge({ trust }: { trust: TrustBlock }) {
  const ok = trust.status === "broker-confirmed";
  const imported = trust.status === "broker-imported";
  const cls = ok ? "ok" : imported ? "imported" : "partial";
  const text = ok
    ? "broker-confirmed ✓"
    : imported
      ? "broker-imported"
      : `bot-computed — ${trust.label}`;
  return (
    <span className={`trust-badge ${cls}`} title={trust.label} data-testid="trust-badge">
      {text}
    </span>
  );
}

// --- SIM-05 paper-mode banner ------------------------------------------------

export function PaperBanner({ mode }: { mode: "paper" | "live" }) {
  if (mode !== "paper") return null;
  return (
    <div className="demo-note" data-testid="paper-banner" role="status">
      ○ PAPER — simulator results (SIM-05). Not broker-confirmed live trading.
    </div>
  );
}

// --- RPT-10 CSV export link (a plain download link, never a fetch) ---------

export function CsvButton({
  table, period, label,
}: {
  table: "daily" | "entries" | "corrections";
  period: ReportPeriod;
  label?: string;
}) {
  return (
    <a
      className="btn csv-btn"
      href={api.reportsCsvUrl(table, period)}
      download
      data-testid={`csv-${table}`}
    >
      {label ?? `Export ${table} CSV`}
    </a>
  );
}

// --- an honest "not yet captured" placeholder (never a fabricated 0/—) ------

export function GapNote({ children }: { children: React.ReactNode }) {
  return (
    <p className="gap-note" data-testid="gap-note">
      {children}
    </p>
  );
}
