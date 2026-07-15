// CAL-08/UI-30 (v1.71, doc 11) — the Calendar tab. A year view (12-month ET
// grid, UI-27 route pattern) of market events with operator NO-TRADE tags and
// standing category rules. NO TRADING LOGIC LIVES HERE (UI-03): every tag,
// rule, and import is a plain read/write against the slice-1 /calendar/*
// endpoints — this only renders what CalendarStore's fold says and posts the
// operator's intent, exactly like every other panel in this app.
//
// ET DAY IDENTITY (DAY-03): every day key this page reads/writes is the
// backend's own "YYYY-MM-DD" ET string; "today" for the year-grid highlight
// is `etDateOf()` (time.ts) — the SAME ET-zone conversion the schedule panel's
// local-echo hints use — NEVER the browser's local calendar date.
import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { etDateOf } from "../time";
import type { CalendarData, CalendarStaleness, CalendarTag } from "../types";

// CAL-01's KNOWN_CATEGORIES (domain/trading_calendar.py), hand-mirrored ONLY
// so the import dialog and the Rules/staleness panels can list every category
// before any import exists — same convention as api.ts's STOP_PCT_SET. The
// BACKEND is the tier authority (GET /calendar's per-category `tier`, from
// tier_for_category): every tier rendered on this page is taken from that
// payload when the category has an import; the `fallback_tier` below is used
// ONLY for a never-imported category (the payload carries no row for it) and
// is display-cosmetic — nothing here ever chooses whether a day trades.
export const CALENDAR_CATEGORIES: { name: string; fallback_tier: 1 | 2 }[] = [
  { name: "FOMC", fallback_tier: 1 },
  { name: "CPI", fallback_tier: 1 },
  { name: "NFP", fallback_tier: 1 },
  { name: "PPI", fallback_tier: 1 },
  { name: "PCE", fallback_tier: 1 },
  { name: "GDP", fallback_tier: 1 },
  { name: "FED_SPEAKER", fallback_tier: 2 },
];

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = Array.from({ length: 12 }, (_, i) => i + 1);

function daysInMonth(year: number, month: number): number {
  return new Date(year, month, 0).getDate();
}

// Monday-first weekday index for the 1st of the month — pure calendar math on
// an ET date STRING (never a browser Date-from-local-parts round trip).
function mondayIndexOfFirst(year: number, month: number): number {
  const jsDay = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  return (jsDay + 6) % 7;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

interface DayEvent {
  category: string;
  tier: 1 | 2;
}

interface DayInfo {
  date: string;
  tag?: CalendarTag;
  events: DayEvent[];
}

/** Every day (this year) that carries a tag or an imported event, keyed by
 * ET date string. A day with neither is simply absent — CAL-01: "a day with
 * no data shows no events, never fabricated ones." */
function buildDayIndex(data: CalendarData | null, year: number): Map<string, DayInfo> {
  const idx = new Map<string, DayInfo>();
  if (!data?.available) return idx;
  const prefix = `${year}-`;
  for (const [day, tag] of Object.entries(data.tags ?? {})) {
    if (!day.startsWith(prefix)) continue;
    idx.set(day, { date: day, tag, events: [] });
  }
  for (const [category, s] of Object.entries(data.staleness ?? {})) {
    // `dates` is a slice-2 ADDITIVE field (final review, 2026-07-15):
    // additive-field discipline means tolerating its absence — a backend
    // that predates it must degrade to "no markers for this category",
    // never a thrown iteration that blanks the whole tab.
    for (const day of s.dates ?? []) {
      if (!day.startsWith(prefix)) continue;
      const entry = idx.get(day) ?? { date: day, events: [] };
      entry.events.push({ category, tier: s.tier });
      idx.set(day, entry);
    }
  }
  return idx;
}

export function CalendarPage() {
  const [data, setData] = useState<CalendarData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const today = etDateOf();
  const [year, setYear] = useState<number>(() => Number(today.slice(0, 4)));
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);

  const load = async () => {
    try {
      setData(await api.getCalendar());
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dayIndex = useMemo(() => buildDayIndex(data, year), [data, year]);
  const selectedInfo = selectedDay ? dayIndex.get(selectedDay) ?? { date: selectedDay, events: [] } : null;

  return (
    <div className="calendar-page" data-testid="calendar-page">
      <h1>Calendar</h1>

      {loadError && <div className="banner-error">Could not load the calendar — {loadError}</div>}
      {data && !data.available && (
        <p className="gap-note" data-testid="calendar-unwired">
          Calendar not wired — no NO-TRADE tags are enforced (CAL-07: an unimported/unwired
          calendar blocks nothing).
        </p>
      )}

      {data?.available && <StalenessBanner staleness={data.staleness ?? {}} year={year} />}

      {data?.available && (
        <div className="calendar-layout">
          <section className="calendar-main">
            <div className="calendar-year-nav" data-testid="calendar-year-nav">
              <button type="button" className="btn" aria-label="previous year" onClick={() => setYear((y) => y - 1)}>
                ◀
              </button>
              <span className="calendar-year-label">{year}</span>
              <button type="button" className="btn" aria-label="next year" onClick={() => setYear((y) => y + 1)}>
                ▶
              </button>
            </div>

            <div className="calendar-months" data-testid="calendar-months">
              {MONTHS.map((month) => (
                <MonthGrid
                  key={month}
                  year={year}
                  month={month}
                  today={today}
                  dayIndex={dayIndex}
                  selected={selectedDay}
                  onSelect={setSelectedDay}
                />
              ))}
            </div>

            <div className="calendar-legend" data-testid="calendar-legend">
              <span className="cal-evt cal-evt-tier1" aria-hidden>●</span> Tier 1 (official schedule)
              <span className="cal-evt cal-evt-tier2" aria-hidden>▲</span> Tier 2 (best-effort, Fed speaker)
              <span className="cal-tag-mark origin-manual" aria-hidden>■</span> Tagged NO-TRADE (manual)
              <span className="cal-tag-mark origin-auto" aria-hidden>◆</span> Tagged NO-TRADE (auto, standing rule)
            </div>
          </section>

          <aside className="calendar-side">
            <RulesPanel rules={data.standing_rules ?? {}} staleness={data.staleness ?? {}}
                        onChanged={load} />
            <DayDetail day={selectedDay} info={selectedInfo} onChanged={load} />
            <button type="button" className="btn" onClick={() => setImportOpen(true)}>
              Import events…
            </button>
          </aside>
        </div>
      )}

      {importOpen && <ImportDialog onCancel={() => setImportOpen(false)}
                                   onImported={() => { setImportOpen(false); void load(); }} />}
    </div>
  );
}

function MonthGrid({
  year, month, today, dayIndex, selected, onSelect,
}: {
  year: number;
  month: number;
  today: string;
  dayIndex: Map<string, DayInfo>;
  selected: string | null;
  onSelect: (day: string) => void;
}) {
  const monthKey = `${year}-${pad2(month)}`;
  const firstIdx = mondayIndexOfFirst(year, month);
  const total = daysInMonth(year, month);
  const cells: (string | null)[] = [
    ...Array.from({ length: firstIdx }, () => null),
    ...Array.from({ length: total }, (_, i) => `${monthKey}-${pad2(i + 1)}`),
  ];

  return (
    <div className="calendar-month" data-testid={`calendar-month-${monthKey}`}>
      <div className="calendar-month-label">{monthKey}</div>
      <div className="calendar-weekday-row" aria-hidden="true">
        {WEEKDAY_LABELS.map((w) => <span key={w} className="calendar-weekday">{w}</span>)}
      </div>
      <div className="calendar-grid">
        {cells.map((date, i) => {
          if (date === null) return <span key={`filler-${i}`} className="calendar-day filler" aria-hidden />;
          const info = dayIndex.get(date);
          const isToday = date === today;
          const tag = info?.tag;
          const tier1 = info?.events.some((e) => e.tier === 1) ?? false;
          const tier2 = info?.events.some((e) => e.tier === 2) ?? false;
          const cls = [
            "calendar-day",
            isToday ? "today" : "",
            tag ? `tagged origin-${tag.origin}` : "",
            selected === date ? "selected" : "",
          ].filter(Boolean).join(" ");
          const label = [
            date,
            isToday ? "today" : "",
            tag ? `tagged NO-TRADE: ${tag.label} (${tag.origin})` : "",
            tier1 ? "tier-1 event" : "",
            tier2 ? "tier-2 event (best-effort)" : "",
          ].filter(Boolean).join(", ");
          return (
            <button
              type="button"
              key={date}
              className={cls}
              data-testid={`cal-day-${date}`}
              aria-label={label}
              aria-pressed={selected === date}
              onClick={() => onSelect(date)}
            >
              <span className="calendar-day-num">{Number(date.slice(-2))}</span>
              <span className="calendar-day-marks" aria-hidden>
                {tag && (
                  <span className={`cal-tag-mark origin-${tag.origin}`} data-testid={`cal-tag-mark-${date}`}>
                    {tag.origin === "manual" ? "■" : "◆"}
                  </span>
                )}
                {tier1 && <span className="cal-evt cal-evt-tier1" data-testid={`cal-evt-tier1-${date}`}>●</span>}
                {tier2 && <span className="cal-evt cal-evt-tier2" data-testid={`cal-evt-tier2-${date}`}>▲</span>}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// CAL-02: staleness is DISPLAYED, never hidden — every known category always
// gets a row, even one with no import at all ("no data imported", never a
// blank meaning nothing happens). Never blocks (CAL-07) — display only.
function StalenessBanner({ staleness, year }: { staleness: Record<string, CalendarStaleness>; year: number }) {
  return (
    <div className="calendar-staleness" data-testid="calendar-staleness">
      {CALENDAR_CATEGORIES.map(({ name }) => {
        const s = staleness[name];
        if (!s) {
          return (
            <div key={name} className="cal-staleness-row none" data-testid={`staleness-${name}`}>
              <strong>{name}</strong> — no data imported
            </div>
          );
        }
        const horizonYear = s.horizon ? Number(s.horizon.slice(0, 4)) : null;
        const beyondHorizon = horizonYear !== null && year > horizonYear;
        return (
          <div key={name} className={`cal-staleness-row ${s.stale ? "stale" : ""}`} data-testid={`staleness-${name}`}>
            <strong>{name}</strong> — dates loaded through {s.horizon ?? "—"}
            {s.stale && <span className="cal-stale-badge" data-testid={`stale-badge-${name}`}>⚠ stale import</span>}
            {beyondHorizon && (
              <span className="cal-no-data-badge" data-testid={`no-data-${name}-${year}`}>
                no data imported for {year}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// CAL-03/04: click a day → its events + tag/untag control + label editor.
// The two-step removal affordance (CAL-04) needs NO special dual-layer
// detection here: `info.tag.origin` is simply whatever the backend's
// layered-removal fold currently says (slice 1) — removing once and
// re-fetching is what SURFACES the underlying auto-tag, if any, exactly as
// the operator experiences it at the broker gate.
function DayDetail({
  day, info, onChanged,
}: {
  day: string | null;
  info: { date: string; tag?: CalendarTag; events: DayEvent[] } | null;
  onChanged: () => void;
}) {
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLabel(info?.tag?.label ?? day ?? "");
    setError(null);
  }, [day, info?.tag?.label]);

  if (!day || !info) {
    return (
      <div className="calendar-day-detail" data-testid="calendar-day-detail-empty">
        <p className="muted">Click a day to see its events and tag it NO-TRADE.</p>
      </div>
    );
  }

  async function tag() {
    setBusy(true);
    setError(null);
    try {
      await api.tagCalendarDay(day!, label);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function untag() {
    setBusy(true);
    setError(null);
    try {
      await api.untagCalendarDay(day!);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  const tag_ = info.tag;
  // CAL-04's own two-step wording: a manual layer removes first; if the day
  // stays tagged afterward (re-read from the backend), it was ALSO covered
  // by a standing rule's auto-tag, and the SAME button now suppresses that.
  const removeLabel = tag_?.origin === "manual" ? "Remove manual tag" : "Suppress auto-tag (rule stays)";

  return (
    <div className="calendar-day-detail" data-testid="calendar-day-detail">
      <h3>{day}</h3>
      {info.events.length === 0 ? (
        <p className="muted">No imported events on this day.</p>
      ) : (
        <ul className="calendar-day-events">
          {info.events.map((e) => (
            <li key={e.category} className={`cal-evt-row tier${e.tier}`}>
              {e.category} <span className="cal-tier-chip">tier {e.tier}{e.tier === 2 ? " (best-effort)" : ""}</span>
            </li>
          ))}
        </ul>
      )}

      {tag_ && (
        <p className={`cal-tag-badge origin-${tag_.origin}`} data-testid="calendar-day-tag-badge">
          Tagged NO-TRADE: <strong>{tag_.label}</strong> ({tag_.origin === "manual" ? "manual" : "auto — standing rule"})
        </p>
      )}

      <label className="field">
        <span>Label</span>
        <input aria-label="tag label" value={label} onChange={(e) => setLabel(e.target.value)} />
      </label>

      <div className="calendar-day-actions">
        <button type="button" className="btn primary" disabled={busy} onClick={() => void tag()}>
          {tag_ ? "Save label" : "Tag NO-TRADE"}
        </button>
        {tag_ && (
          <button type="button" className="btn danger" disabled={busy} onClick={() => void untag()}>
            {removeLabel}
          </button>
        )}
      </div>
      {error && <p className="msg err" role="alert">{error}</p>}
    </div>
  );
}

// CAL-04: "always block <category>" standing rules — every change effective
// immediately (no restart), through the slice-1 endpoints.
function RulesPanel({
  rules, staleness, onChanged,
}: {
  rules: Record<string, string | null>;
  staleness: Record<string, CalendarStaleness>;
  onChanged: () => void;
}) {
  const [busyCategory, setBusyCategory] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The BACKEND is the tier authority (2026-07-15 review): read the tier off
  // GET /calendar's own per-category payload whenever the category has an
  // import; the hand-mirrored fallback_tier covers only a never-imported
  // category, which has no payload row to read from.
  const tierOf = (name: string, fallback: 1 | 2): 1 | 2 => staleness[name]?.tier ?? fallback;

  async function toggle(category: string, on: boolean) {
    setBusyCategory(category);
    setError(null);
    try {
      if (on) await api.setCalendarRule(category);
      else await api.removeCalendarRule(category);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusyCategory(null);
    }
  }

  return (
    <div className="calendar-rules" data-testid="calendar-rules">
      <h3>Standing rules</h3>
      <ul className="calendar-rules-list">
        {CALENDAR_CATEGORIES.map(({ name, fallback_tier }) => {
          const on = name in rules;
          return (
            <li key={name}>
              <label className="field floor-toggle">
                <input
                  type="checkbox"
                  aria-label={`always block ${name}`}
                  checked={on}
                  disabled={busyCategory === name}
                  onChange={(e) => void toggle(name, e.target.checked)}
                />
                <span>Always block {name}{tierOf(name, fallback_tier) === 2 ? " (tier 2)" : ""}</span>
              </label>
            </li>
          );
        })}
      </ul>
      {error && <p className="msg err" role="alert">{error}</p>}
    </div>
  );
}

// CAL-01: operator-triggered, auth-gated paste-table import — never a
// fabricated schedule; exactly the dates the operator supplies.
function ImportDialog({ onCancel, onImported }: { onCancel: () => void; onImported: () => void }) {
  const [category, setCategory] = useState(CALENDAR_CATEGORIES[0].name);
  const [datesText, setDatesText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const dates = datesText.split(/[\n,]/).map((d) => d.trim()).filter(Boolean);
    setBusy(true);
    setError(null);
    try {
      await api.importCalendarEvents({ category, dates });
      onImported();
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Import calendar events">
      <div className="modal">
        <h3>Import calendar events</h3>
        <p className="sub">Paste one ET date (YYYY-MM-DD) per line — never fabricated (CAL-01).</p>

        <label className="field">
          <span>Category</span>
          <select aria-label="import category" value={category} onChange={(e) => setCategory(e.target.value)}>
            {CALENDAR_CATEGORIES.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
          </select>
        </label>

        <label className="field">
          <span>Dates</span>
          <textarea
            aria-label="import dates"
            value={datesText}
            placeholder={"2026-07-29\n2026-09-16"}
            onChange={(e) => setDatesText(e.target.value)}
          />
        </label>

        {error && <p className="msg err" role="alert">{error}</p>}

        <div className="modal-actions">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
          <button type="button" className="btn primary" onClick={() => void submit()} disabled={busy}>
            {busy ? "Importing…" : "Import"}
          </button>
        </div>
      </div>
    </div>
  );
}
