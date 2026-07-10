import { useEffect, useRef } from "react";
import type { ActivityLine } from "../types";

// Live activity feed — a human-readable stream of the day's events, newest
// first (doc 05 §8). Presentation only; the backend formats each line.

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
          {activity.map((a, i) => (
            <li key={i} ref={i === 0 ? ref : undefined} className="feed-line">
              <span className="feed-icon">{a.icon}</span>
              <span className="feed-label">{a.label}</span>
              {a.entry && <span className="feed-entry">{a.entry}</span>}
              {a.detail && <span className="feed-detail">{a.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
