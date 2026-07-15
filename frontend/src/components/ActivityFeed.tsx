import { useEffect, useRef } from "react";
import { groupActivityByDay } from "../activityDays";
import type { ActivityLine } from "../types";

// Live activity feed — a human-readable stream of the day's events, newest
// first (doc 05 §8). Presentation only; the backend formats each line. A
// day-separator row (operator request 2026-07-15) is inserted before the
// first item of each new ET trading day (DAY-03) — see activityDays.ts for
// the grouping/fallback logic.

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
              <li key={`day-${row.day}`} className="feed-day-separator">
                {row.label}
              </li>
            ) : (
              <li key={i} ref={i === firstItemIndex ? ref : undefined} className="feed-line">
                <span className="feed-icon">{row.item.icon}</span>
                <span className="feed-label">{row.item.label}</span>
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
