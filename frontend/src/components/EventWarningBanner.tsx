// CAL-11/UI-34 (v1.84, doc 11) — the Trading tab's dismissable AMBER warning
// banner for upcoming calendar events. Purely informational (CAL-11(1)): this
// component changes NO gate and blocks NO entry — it only fetches
// GET /calendar/warnings and POSTs a dismiss intent, exactly like every other
// display-only panel in this app (UI-03). Self-contained: its own fetch on an
// independent interval, no wiring into useLiveBot's WS/poll snapshot stream.
import { useEffect, useState } from "react";
import { api } from "../api";
import { Tooltip } from "./Tooltip";
import type { EventWarning } from "../types";

const REFRESH_MS = 60_000; // this data changes at most daily -- no need for anything faster

export function EventWarningBanner() {
  const [warnings, setWarnings] = useState<EventWarning[]>([]);
  const [available, setAvailable] = useState(true);

  const load = async () => {
    try {
      const data = await api.getCalendarWarnings();
      setAvailable(data.available);
      // CAL-11(4): multiple events stack, nearest-first -- the backend
      // already sorts ascending by proximity_tier; render in given order,
      // never resort/re-derive that ordering client-side.
      setWarnings(data.warnings);
    } catch {
      // leave the last known list in place; the next refresh will retry
    }
  };

  useEffect(() => {
    let alive = true;
    (async () => {
      if (alive) await load();
    })();
    const id = window.setInterval(() => { void load(); }, REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function dismiss(w: EventWarning) {
    // Optimistic removal for snappy feedback, then re-fetch to stay honest
    // against the persisted (REC-07) dismissal store.
    setWarnings((prev) => prev.filter((x) =>
      !(x.category === w.category && x.event_date === w.event_date && x.proximity_tier === w.proximity_tier)));
    try {
      await api.dismissCalendarWarning(w.category, w.event_date, w.proximity_tier);
    } finally {
      void load();
    }
  }

  if (!available || warnings.length === 0) return null;

  return (
    <div className="event-warning-banner" data-testid="event-warning-banner">
      {warnings.map((w) => {
        const rowTestId = `event-warning-row-${w.category}-${w.event_date}-${w.proximity_tier}`;
        return (
          <div key={rowTestId} className="event-warning-row" data-testid={rowTestId}>
            <span className="event-warning-text">{w.human_label}</span>
            {w.best_effort && (
              <>
                <span className="event-warning-best-effort"> (best-effort)</span>
                <Tooltip
                  id={`event-warning-best-effort-${w.category}-${w.event_date}-${w.proximity_tier}`}
                  testId={`event-warning-best-effort-tip-${w.category}-${w.event_date}-${w.proximity_tier}`}
                  label={`why ${w.label} is best-effort`}
                  content="Fed speaker events have no official published schedule (unlike FOMC/CPI/PPI/NFP/PCE/GDP) -- timing and even whether the event occurs as expected can't be guaranteed the same way."
                />
              </>
            )}
            <button
              type="button"
              className="event-warning-dismiss"
              aria-label="dismiss warning"
              onClick={() => void dismiss(w)}
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
