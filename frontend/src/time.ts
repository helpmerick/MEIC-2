// Timezone helpers for the schedule panel.
//
// Entry times are ET (America/New_York) — the bot operates in ET (DAY-03). The
// operator may sit anywhere, so we show each ET time's equivalent in their LOCAL
// timezone, read live from the browser. DST is handled automatically because
// Intl.DateTimeFormat resolves the correct wall-clock for any instant in a named
// zone — no offset tables, no manual DST logic.

export const ET_ZONE = "America/New_York";

/** The operator's own IANA zone, e.g. "Europe/London". */
export function localZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

/** A friendly label for a zone: "Europe/London" -> "London", "UTC" -> "UTC". */
export function zoneLabel(zone = localZone()): string {
  const city = zone.split("/").pop() ?? zone;
  return city.replace(/_/g, " ");
}

// Offset (ms) of `zone` at a given UTC instant = (wall clock in zone) - utc.
function offsetMs(utcMs: number, zone: string): number {
  const p: Record<string, string> = {};
  for (const part of new Intl.DateTimeFormat("en-US", {
    timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).formatToParts(new Date(utcMs))) {
    p[part.type] = part.value;
  }
  // "24" for midnight can appear in some engines; normalise to 0.
  const hour = p.hour === "24" ? 0 : +p.hour;
  const asIfUTC = Date.UTC(+p.year, +p.month - 1, +p.day, hour, +p.minute, +p.second);
  return asIfUTC - utcMs;
}

/**
 * "HH:MM" of a full ISO instant (e.g. /day/status's next_entry_at) rendered in
 * `zone`; null if unparsable. Unlike `etToZone` this needs no "today" assumption
 * — the instant carries its own date, so a next entry on the far side of a DST
 * switch still converts correctly (UI-24 rollover, operator ruling 2026-07-11).
 */
export function instantToZone(iso: string, zone = localZone()): string | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: zone, hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(d);
}

/** The ET calendar date of an instant, "YYYY-MM-DD". */
function etDateOf(d: Date): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: ET_ZONE, year: "numeric", month: "2-digit", day: "2-digit",
  }).format(d);
}

/**
 * Short ET weekday ("Mon") of an instant that falls on a DIFFERENT ET calendar
 * date than now — null when it's still today (or unparsable). UI-24: the
 * weekend/holiday rollover labels the day so a Saturday reader can't mistake
 * Monday's entry for one firing today.
 */
export function etDayLabel(iso: string): string | null {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  if (etDateOf(d) === etDateOf(new Date())) return null;
  return new Intl.DateTimeFormat("en-US", { timeZone: ET_ZONE, weekday: "short" }).format(d);
}

/**
 * A 24-hour "HH:MM" wall-clock time in ET (for TODAY's date) rendered in `zone`.
 * Returns null if the input isn't a valid 24-hour time. DST-aware.
 */
export function etToZone(hhmm: string, zone = localZone()): string | null {
  // Accept either separator: "11:53" or the UK-style "11.53" — both mean 11:53.
  const m = /^(\d{1,2})[.:](\d{2})$/.exec(hhmm.trim());
  if (!m) return null;
  const h = +m[1], min = +m[2];
  if (h > 23 || min > 59) return null;

  const now = new Date();
  // The wall-clock h:min read as if it were UTC, for today's date.
  const wallAsUTC = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), h, min);
  // The real UTC instant whose ET wall clock is h:min today.
  const instant = wallAsUTC - offsetMs(wallAsUTC, ET_ZONE);
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: zone, hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(instant));
}

// A 24-hour "HH:MM" (military) time. 0-23 : 00-59. Leading zero optional on the
// hour; the separator may be a colon OR a dot ("11:53" / "11.53" both = 11:53) —
// people write times both ways. am/pm and out-of-range are still refused. The
// backend canonicalises to "HH:MM" on save; this only decides the inline hint.
const MILITARY = /^([01]?\d|2[0-3])[.:][0-5]\d$/;
export function isMilitaryTime(hhmm: string): boolean {
  return MILITARY.test(hhmm.trim());
}

/** Canonical "HH:MM" for a valid military time (dot or colon), else the input. */
export function canonicalTime(hhmm: string): string {
  const m = /^(\d{1,2})[.:](\d{2})$/.exec(hhmm.trim());
  if (!m) return hhmm;
  return `${m[1].padStart(2, "0")}:${m[2]}`;
}

// Regular trading hours in ET: an entry time is only valid while the market is
// open, 09:30-16:00 ET (operator ruling — the time VALUE, independent of when the
// schedule is composed). The DAY-02 30-min-before-close buffer is a SEPARATE gate
// the backend still applies on top of this. Backend is authoritative; this only
// decides the inline hint.
export const RTH_OPEN_LABEL = "09:30";
export const RTH_CLOSE_LABEL = "16:00";
export function withinMarketHours(hhmm: string): boolean {
  const m = /^(\d{1,2})[.:](\d{2})$/.exec(hhmm.trim());
  if (!m) return false;
  const mins = +m[1] * 60 + +m[2];
  return mins >= 9 * 60 + 30 && mins <= 16 * 60;
}
