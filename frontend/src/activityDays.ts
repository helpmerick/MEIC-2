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
 * Feed items (newest-first) grouped under one separator row per ET day.
 *
 * The journal is WRITE-TIME monotonic, but an event's `at` can be BACKDATED
 * routinely — not hypothetically: the EOD look-back captures settlements a
 * day late, stamping `at` with the broker's own settlement instant (e.g.
 * 07-13T20:00Z) while the event is journaled on 07-14. So day-of-item is NOT
 * monotonic in journal order, and a naive single-pass walk fragments days
 * into interleaved repeated headers (production bug, operator screenshot
 * 2026-07-15: "Dates should always be continuous"). Hence:
 *
 *   1. derive each item's day in ORIGINAL newest-first journal order, with
 *      the inheritance fallback (an item with no derivable day inherits its
 *      preceding JOURNAL neighbour's day — resolved BEFORE any reordering,
 *      so inheritance keeps its journal-context meaning);
 *   2. stable-sort by derived day DESCENDING (newest day first), preserving
 *      relative journal order within each day. Null-day items travel with
 *      the day they inherited; a leading null-day run with nothing to
 *      inherit stays at the top, headerless (never a fabricated date);
 *   3. emit one separator per day — provably unique, since the sort makes
 *      each day contiguous.
 *
 * A pure function of the current activity array: live arrivals (ws/poll)
 * regroup correctly on every call, including a brand-new day's first item
 * getting its own separator at the top.
 */
export function groupActivityByDay(activity: ActivityLine[]): ActivityRow[] {
  // 1. day per item, in journal order, inheritance resolved here and only here.
  let inherit: string | null = null;
  const withDay = activity.map((item, i) => {
    const day: string | null = dayOfActivityItem(item) ?? inherit;
    inherit = day;
    return { item, day, i };
  });

  // 2. stable sort: day descending; ties keep journal order. A null day can
  // only be a leading run (inheritance fills every later gap), and it sorts
  // before everything so it stays at the top, headerless.
  const sorted = [...withDay].sort((a, b) => {
    if (a.day === b.day) return a.i - b.i;
    if (a.day === null) return -1;
    if (b.day === null) return 1;
    return a.day < b.day ? 1 : -1; // ISO YYYY-MM-DD sorts lexicographically
  });

  // 3. one separator per (now-contiguous) day.
  const rows: ActivityRow[] = [];
  let lastDay: string | null = null;
  for (const { item, day } of sorted) {
    if (day !== null && day !== lastDay) {
      rows.push({ kind: "separator", day, label: formatDaySeparator(day) });
      lastDay = day;
    }
    rows.push({ kind: "item", item });
  }
  return rows;
}
