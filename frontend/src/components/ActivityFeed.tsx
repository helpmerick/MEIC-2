import { useEffect, useRef } from "react";
import { ACTIVITY_VOCABULARY } from "../activityVocabulary";
import { groupActivityByDay } from "../activityDays";
import { ET_ZONE, instantToZone } from "../time";
import { Tooltip } from "./Tooltip";
import type { ActivityLine } from "../types";

// Live activity feed — a human-readable stream of the day's events, newest
// first (doc 05 §8). Presentation only; the backend formats each line. A
// day-separator row (operator request 2026-07-15) is inserted before the
// first item of each new ET trading day (DAY-03) — see activityDays.ts for
// the grouping/fallback logic.
//
// UI-31 (v1.73, queue slice 5) adds three things on top of that separator:
// (1) the separator itself is now STICKY within `.feed`'s own scroll
// container (`.feed` carries `overflow-y: auto` and each separator is
// rendered as a DIRECT <li> child of it, so no wrapper needs fixing — the
// `<ul>` already IS the nearest scrolling ancestor); the inline style below
// is set directly (not only via the CSS class) because vitest.config.ts runs
// with `css: false`, so a jsdom assertion on computed position must read an
// inline style, not an external stylesheet rule. (2) every row shows its own
// ET wall-clock time, derived from the SAME `at` (UTC) instant the day
// grouping already uses, via the shared `instantToZone` helper (never a
// second, drifting time conversion — this codebase has been bitten three
// times reinventing that). (3) every row gets a hover/focus/tap tooltip
// (v1.63 standard) explaining its event in plain English, looked up by the
// row's own `type` key in activityVocabulary.ts; a row with no `type` (a
// hand-built caller that predates this field) or an unrecognised one simply
// renders without a tooltip — never a fabricated explanation.

export function ActivityFeed({ activity }: { activity: ActivityLine[] }) {
  const topKey = activity.length ? `${activity[0].icon}${activity[0].label}${activity[0].entry}${activity[0].detail}` : "";
  const ref = useRef<HTMLLIElement>(null);
  const prev = useRef(topKey);
  useEffect(() => {
    if (prev.current !== topKey && ref.current) {
      ref.current.classList.remove("flash-in");
      void ref.current.offsetWidth;
      ref.current.classList.add("flash-in");
      prev.current = topKey;
    }
  }, [topKey]);

  const rows = groupActivityByDay(activity);
  const firstItemIndex = rows.findIndex((r) => r.kind === "item");

  return (
    <section className="card feed-card">
      <div className="card-head">
        <h2>Activity</h2>
        <span className="muted">live</span>
      </div>
      {activity.length === 0 ? (
        <p className="muted">Nothing has happened yet.</p>
      ) : (
        <ul className="feed">
          {rows.map((row, i) =>
            row.kind === "separator" ? (
              <li
                key={`day-${row.day}`}
                className="feed-day-separator"
                style={{ position: "sticky", top: 0, zIndex: 1 }}
              >
                {row.label}
              </li>
            ) : (
              <li key={i} ref={i === firstItemIndex ? ref : undefined} className="feed-line">
                <span className="feed-icon">{row.item.icon}</span>
                {row.item.at && instantToZone(row.item.at, ET_ZONE) && (
                  <span className="feed-time">{instantToZone(row.item.at, ET_ZONE)}</span>
                )}
                <span className="feed-label">{row.item.label}</span>
                {row.item.type && ACTIVITY_VOCABULARY[row.item.type] && (
                  <Tooltip
                    id={`feed-tip-content-${i}`}
                    content={ACTIVITY_VOCABULARY[row.item.type]}
                    testId={`feed-tip-${i}`}
                    label={`explain: ${row.item.label}`}
                  />
                )}
                {row.item.entry && <span className="feed-entry">{row.item.entry}</span>}
                {row.item.detail && <span className="feed-detail">{row.item.detail}</span>}
              </li>
            )
          )}
        </ul>
      )}
    </section>
  );
}
