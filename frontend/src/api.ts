// API client — the ONLY place the frontend talks to the backend.
// No trading logic lives here or anywhere in the frontend (UI-03): it reads
// state and sends commands; the backend validates everything.

import type {
  ActivityLine, CalendarData, DailyRow, DayReport, DayReportDetail, DayStatus, EntryCard, FirePreview,
  FireResult, FloorCandidates, GettingStartedData, GuideData, ManualSimulation, PanelState, Preflight,
  ReportSummary, ScheduleRow, ScheduleView,
} from "./types";

// NFR-06: when the operator has set an api_token, mutating requests must carry
// it. Read it from localStorage; empty on a plain localhost bind (paper demo).
const TOKEN_KEY = "meic_api_token";
function apiToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}
export const getApiToken = (): string => apiToken();
export function setApiToken(value: string): void {
  const v = value.trim();
  if (v) localStorage.setItem(TOKEN_KEY, v);
  else localStorage.removeItem(TOKEN_KEY);
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { accept: "application/json" } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return (await r.json()) as T;
}

async function getText(path: string): Promise<string> {
  const r = await fetch(path, { headers: { accept: "text/csv" } });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.text();
}

// RPT-01 period buckets — exactly one of these narrows the scope; none = all-time.
export interface ReportPeriod {
  period?: "today" | "all";
  day?: string;    // YYYY-MM-DD
  month?: string;  // YYYY-MM
  year?: string;   // YYYY
}

function reportsQueryString(p: ReportPeriod): string {
  const q = new URLSearchParams();
  if (p.period) q.set("period", p.period);
  if (p.day) q.set("day", p.day);
  if (p.month) q.set("month", p.month);
  if (p.year) q.set("year", p.year);
  return q.toString();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  const token = apiToken();
  if (token) headers["x-api-token"] = token;
  const r = await fetch(path, {
    method: "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new ApiError(r.status, detail?.detail ?? r.statusText);
  }
  return (await r.json()) as T;
}

// CAL-03/04 (v1.71): DELETE /calendar/tag/{day} and /calendar/rule/{category}
// carry the day/category in the PATH, no body — same auth header as `post`.
async function del<T>(path: string): Promise<T> {
  const headers: Record<string, string> = {};
  const token = apiToken();
  if (token) headers["x-api-token"] = token;
  const r = await fetch(path, { method: "DELETE", headers });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    throw new ApiError(r.status, detail?.detail ?? r.statusText);
  }
  return (await r.json()) as T;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

export const api = {
  // NFR-06: verify the User Password without side effects. 200 = accepted (or none
  // required); throws ApiError(401) when the password is wrong or missing.
  authCheck: () => post<{ ok: boolean }>("/auth/check"),
  getState: () => get<PanelState>("/state"),
  getReport: () => get<DayReport>("/report"),
  getEntries: () => get<EntryCard[]>("/entries"),
  getActivity: () => get<ActivityLine[]>("/activity"),
  arm: () => post<PanelState>("/arm"),
  disarm: () => post<PanelState>("/disarm"),
  stopTrading: (on: boolean) => post<PanelState>(`/stop-trading?on=${on}`),
  confirmLive: (on: boolean) => post<PanelState>(`/confirm-live?on=${on}`),
  updateConfig: (patch: Record<string, unknown>) =>
    post<{ accepted: Record<string, unknown> }>("/config", patch),
  closeEntry: (entryId: string) =>
    post<{ result: string }>(`/close/${encodeURIComponent(entryId)}`),
  // --- v1.58 TPF/TPT: set/raise/lower/clear per entry (UI-13/14/15) ---------
  // Server-side gap validation is authoritative (UI-03): a violating level
  // throws ApiError(422) with a precise reason, never silently clamped.
  setTpf: (entryId: string, level: number) =>
    post<{ result: string; entry_id?: string; level?: number; reason?: string }>(
      `/entries/${encodeURIComponent(entryId)}/tpf`, { level }),
  clearTpf: (entryId: string) =>
    post<{ result: string; entry_id: string }>(`/entries/${encodeURIComponent(entryId)}/tpf/clear`),
  setTpt: (entryId: string, level: number) =>
    post<{ result: string; entry_id?: string; level?: number; reason?: string }>(
      `/entries/${encodeURIComponent(entryId)}/tpt`, { level }),
  clearTpt: (entryId: string) =>
    post<{ result: string; entry_id: string }>(`/entries/${encodeURIComponent(entryId)}/tpt/clear`),
  flatten: (confirmation: string) =>
    post<{ result: string; entries?: string[] }>("/flatten", { confirmation }),
  // UC-12 v1.56: LIVE mode requires a typed DRILL confirmation; PAPER needs
  // none (the caller passes "" and the backend simply ignores it there).
  outageDrill: (confirmation: string = "") =>
    post<OutageDrill>("/drill/outage", { outage_seconds: 2, confirmation }),
  modeSwitch: (target: "paper" | "live", confirmation: string) =>
    post<{ staged: boolean; target: string; effective: string }>("/mode-switch", { target, confirmation }),

  // --- UC-02 schedule -------------------------------------------------------
  getSchedule: () => get<ScheduleView>("/schedule"),
  // Validation is entirely server-side (UI-03). A 422 carries EVERY error, so the
  // form can mark each offending cell in one pass.
  saveSchedule: (rows: ScheduleRow[], maxDayRisk: string) =>
    post<ScheduleView & { config_version: string }>("/schedule", {
      rows,
      max_day_risk: maxDayRisk === "" ? null : maxDayRisk,
    }),
  getPreflight: () => get<Preflight>("/preflight"),
  // ENT-10 / UI-24: the day supervisor's watch state — next entry, countdown.
  getDayStatus: () => get<DayStatus>("/day/status"),
  // DAY-01: the Results page Day picker's ◀/▶ arrows — step to the previous/
  // next NYSE trading session, skipping weekends and market holidays.
  adjacentTradingDay: (from: string, dir: "prev" | "next") =>
    get<{ date: string | null }>(`/calendar/adjacent-trading-day?from=${from}&dir=${dir}`),

  // --- ENT-09 manual fire (UI-22) -------------------------------------------
  firePreview: (n: number) => get<FirePreview>(`/entry/${n}/fire-preview`),
  // The press_id came from the preview: confirming twice is ONE attempt.
  // `blackoutAck` (CAL-06, v1.71): the OK dialog's explicit acknowledgment
  // checkbox — omitted entirely on an untagged day (the backend ignores it
  // there anyway; omitting keeps the request identical to every pre-v1.71 call).
  fire: (n: number, pressId: string, blackoutAck?: boolean) =>
    post<FireResult>(`/entry/${n}/fire`, {
      press_id: pressId, confirmed: true,
      ...(blackoutAck !== undefined ? { blackout_ack: blackoutAck } : {}),
    }),

  // --- ENT-11/UI-25 ad-hoc manual trade --------------------------------------
  // Read-only: places no order, appends no event. POST (not GET) because it
  // still spends a live selector call against real broker/chain data — a
  // budget worth gating behind the same auth/origin middleware as every
  // mutating command.
  manualSimulate: (params: Record<string, unknown>) =>
    post<ManualSimulation>("/manual/simulate", params),
  manualFire: (params: Record<string, unknown> & { press_id: string; confirmed: boolean }) =>
    post<FireResult>("/manual/fire", params),
  // ENT-09b v1.57: the ad-hoc ▶ dialog's floor dropdowns, for the row's OWN
  // parameters (target/wing/width feed the reachable-set computation).
  manualFloorCandidates: (params: Record<string, unknown>) =>
    post<FloorCandidates>("/manual/floor-candidates", params),

  // --- RPT-09/10 results dashboard (doc 10) ---------------------------------
  // Read-only, origin-open exactly like /state and /report (RPT-10).
  getReportSummary: (p: ReportPeriod = {}) => {
    const qs = reportsQueryString(p);
    return get<ReportSummary>(`/reports/summary${qs ? `?${qs}` : ""}`);
  },
  getReportDay: (isoDate: string) =>
    get<DayReportDetail>(`/reports/day/${encodeURIComponent(isoDate)}`),
  // A plain <a href> download link, not a fetch — the endpoint returns a
  // text/csv attachment (RPT-10); the browser handles the download itself.
  reportsCsvUrl: (table: "daily" | "entries" | "corrections", p: ReportPeriod = {}) => {
    const qs = reportsQueryString(p);
    return `/reports/csv?table=${table}${qs ? `&${qs}` : ""}`;
  },
  // KNOWN API-SHAPE GAP (reported, not worked around in the backend): RPT-09's
  // equity curve and calendar heatmap need a per-day net-P&L SERIES, but
  // /reports/summary only returns period AGGREGATES — there is no JSON array
  // of {date, net_pnl} in this slice. The only per-day series the backend
  // exposes at all is the "daily" CSV export (RPT-10), built server-side from
  // the exact same `daily_net()` fold RPT-04's metrics use — so this parses
  // that export rather than re-deriving anything. Every value stays the
  // server's own Decimal string; a future slice should add a JSON
  // `daily: [...]` array to GET /reports/summary so this indirection isn't
  // needed.
  // `wins`/`losses` (RPT-09 calendar-heatmap hover) are the 5th/6th CSV
  // columns; blank on a broker-imported day (RPT-16) parses to null here —
  // never coerced to a fabricated 0.
  getDailySeries: async (p: ReportPeriod = {}): Promise<DailyRow[]> => {
    const qs = reportsQueryString(p);
    const text = await getText(`/reports/csv?table=daily${qs ? `&${qs}` : ""}`);
    const lines = text.trim().length ? text.trim().split(/\r?\n/) : [];
    const [, ...rows] = lines; // drop the header row
    return rows
      .filter((line) => line.length > 0)
      .map((line) => {
        const [date, mode, net_pnl, trust, wins, losses, entries] = line.split(",");
        return {
          date, mode, net_pnl, trust,
          wins: wins ? Number(wins) : null,
          losses: losses ? Number(losses) : null,
          // UI-26a: the hover box's entries count — same blank-means-null
          // honesty as wins/losses on a broker-imported day.
          entries: entries ? Number(entries) : null,
        };
      });
  },

  // --- CAL-01..08 trading calendar (doc 11, slice 2) ------------------------
  // Read-only, origin-open like every other read model (RPT-10 precedent);
  // every mutation below is POST/DELETE, caught by the security middleware's
  // origin/token check exactly like every other command.
  getCalendar: () => get<CalendarData>("/calendar"),
  importCalendarEvents: (params: {
    category: string; dates: string[]; labels?: Record<string, string>; source?: string;
  }) => post<{ result: string; category: string; count: number }>("/calendar/import", params),
  tagCalendarDay: (day: string, label?: string) =>
    post<{ result: string; day: string; label: string }>("/calendar/tag", { day, label }),
  untagCalendarDay: (day: string) =>
    del<{ result: string; day: string }>(`/calendar/tag/${encodeURIComponent(day)}`),
  setCalendarRule: (category: string, label?: string) =>
    post<{ result: string; category: string }>("/calendar/rule", { category, label }),
  removeCalendarRule: (category: string) =>
    del<{ result: string; category: string }>(`/calendar/rule/${encodeURIComponent(category)}`),

  // --- DOC-01..05 how-it-works guide (doc 12, slice 4) -----------------------
  // Read-only, origin-open like every other read model above -- no trading
  // capability, no state mutation (the guide route is pure GET).
  getGuide: () => get<GuideData>("/guide"),

  // --- DOC-06/UI-32 Getting-started tab (doc 12, slice 6, v1.78) -------------
  // Read-only, pure GET, same guarantee as getGuide: the payload is spec/12's
  // own "# GETTING STARTED" section text (names and guidance only, never a
  // live env value or secret -- DOC-06).
  getGettingStarted: () => get<GettingStartedData>("/getting-started"),
};

// UC-12 stop-independence drill evidence (mirrors application/drills.py).
export interface OutageDrill {
  result: "ok";
  outage_seconds: number;
  stops_before: { order_id: string; received_at: string; entry_id: string; leg: string }[];
  stops_after: { order_id: string; received_at: string; entry_id: string; leg: string }[];
  survived: boolean;
  timestamps_unbroken: boolean;
  honesty_note: string;
  // UC-12 v1.56: advisory-only warnings (near-trigger marks / an entry due
  // soon) — never a block, the operator is supervising.
  guidance: string[];
}

// Discrete stop-pct set (UI-04) — in production generated from the config
// schema the backend serves, so UI and backend cannot drift.
export const STOP_PCT_SET: number[] = Array.from({ length: (300 - 95) / 5 + 1 }, (_, i) => 95 + i * 5);

// STP-02 / doc 06 ScheduleDefaults.stop_loss_pct. A new row starts here rather
// than blank: the backend resolves a blank cell to 95 and echoes 95 straight
// back, so a "default" option only ever showed the operator a value the very
// next round-trip replaced with the number anyway.
export const DEFAULT_STOP_PCT = 95;
