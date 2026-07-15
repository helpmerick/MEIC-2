// Day-separator grouping for the Activity feed (operator request 2026-07-15).
//
// The feed is newest-first (doc 05 §8 / ActivityFeed.tsx). Date identity is
// the ET trading day (DAY-03) -- derived from each item's own instant via the
// SAME conversion the Calendar tab uses (etDateOf), never the browser's local
// date. This codebase has been bitten twice by local-date grouping.
import { etDateOf } from "./time";
import type { ActivityLine } from "./types";

export type ActivityRow =
  | { kind: "separator"; day: string; label: string }
  | { kind: "item"; item: ActivityLine };

// entry_id is ET-day-stamped at creation ("2026-07-14#101", ENT-10/11) --
// the day prefix is a reliable fallback when an item has neither `at` nor
// `date` (e.g. CondorProposed).
const ENTRY_DAY_RE = /^(\d{4}-\d{2}-\d{2})#/;

/**
 * The ET trading day an activity item belongs to, or null if it cannot be
 * honestly derived (the caller then inherits the preceding item's day).
 * Fallback chain:
 *   1. the item's own `at` instant, converted to its ET calendar date;
 *   2. else the event's own `date` field (DayArmed, EntryWindowOpened,
 *      EntrySkipped, DayCompleted all carry this instead of `at`);
 *   3. else the entry_id's day prefix;
 *   4. else null -- never fabricated (e.g. ModeSwitchStaged carries none of
 *      the above).
 */
export function dayOfActivityItem(item: ActivityLine): string | null {
  if (item.at) {
    const d = new Date(item.at);
    if (!Number.isNaN(d.getTime())) return etDateOf(d);
  }
  if (item.date) return item.date;
  const m = ENTRY_DAY_RE.exec(item.entry);
  if (m) return m[1];
  return null;
}

/**
 * "Monday 14 July 2026" for a "YYYY-MM-DD" day string. Anchored at noon UTC
 * so the calendar string alone decides the label, clear of any DST edge --
 * independent of the browser's own zone.
 */
export function formatDaySeparator(day: string): string {
  const d = new Date(`${day}T12:00:00Z`);
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC", weekday: "long", day: "numeric", month: "long", year: "numeric",
  }).formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("weekday")} ${get("day")} ${get("month")} ${get("year")}`;
}

/**
 * Feed items (newest-first) with a separator row inserted before the first
 * item of each new ET day. An item whose day cannot be derived (see
 * `dayOfActivityItem`) inherits the PRECEDING item's day -- journal order is
 * chronological, so this is never a fabrication, just an honest carry-over.
 * A pure function of the current activity array: live arrivals (ws/poll)
 * regroup correctly on every call, including a brand-new day's first item
 * getting its own separator at the top.
 *
 * NOTE: relies on the event journal being time-monotonic (append-only, in
 * chronological order) — a future backfill/import that appends out-of-order
 * historical events would need sorting upstream first, or real day-dedup here.
 */
export function groupActivityByDay(activity: ActivityLine[]): ActivityRow[] {
  const rows: ActivityRow[] = [];
  let lastDay: string | null = null;
  for (const item of activity) {
    const day: string | null = dayOfActivityItem(item) ?? lastDay;
    if (day !== null && day !== lastDay) {
      rows.push({ kind: "separator", day, label: formatDaySeparator(day) });
      lastDay = day;
    }
    rows.push({ kind: "item", item });
  }
  return rows;
}
