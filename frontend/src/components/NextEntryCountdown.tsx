import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { ET_ZONE, etDayLabel, etToZone, instantToZone } from "../time";
import type { DayStatus } from "../types";

// UI-24 — display-only (UI-03); the backend's seconds_to_next is authoritative,
// the browser clock only animates between polls.
export function NextEntryCountdown() {
  const [status, setStatus] = useState<DayStatus | null>(null);
  const [, setTick] = useState(0);          // forces a re-render each second to animate
  const lastPoll = useRef<number>(Date.now());

  useEffect(() => {
    let alive = true;

    async function poll() {
      try {
        const s = await api.getDayStatus();
        if (!alive) return;
        setStatus(s);
        lastPoll.current = Date.now();
      } catch {
        // leave the last known status in place; the next poll will retry
      }
    }

    poll();
    const pollId = window.setInterval(poll, 5000);
    const tickId = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => {
      alive = false;
      window.clearInterval(pollId);
      window.clearInterval(tickId);
    };
  }, []);

  if (!status) return null;

  if (!status.armed) {
    return <div className="next-entry idle" data-testid="next-entry">schedule idle — arm to run</div>;
  }

  if (!status.next_entry_at) {
    return <div className="next-entry idle" data-testid="next-entry">no more entries today</div>;
  }

  // The backend's seconds_to_next is authoritative; between polls we only
  // animate it down by however long it's been since the last poll landed.
  const secondsSincePoll = Math.floor((Date.now() - lastPoll.current) / 1000);
  const remaining = Math.max(0, (status.seconds_to_next ?? 0) - secondsSincePoll);

  // next_entry_at is a full ET-offset instant; converting THE INSTANT (not a
  // bare HH:MM read as "today") keeps the local echo right when the next entry
  // is days away — the UI-24 weekend/holiday rollover (operator ruling
  // 2026-07-11) — including across a DST switch. The regex path only remains
  // as a fallback for an unparsable timestamp.
  const hhmm = instantToZone(status.next_entry_at, ET_ZONE)
    ?? (/T(\d{2}):(\d{2})/.exec(status.next_entry_at)?.slice(1).join(":") ?? null);
  const local = instantToZone(status.next_entry_at) ?? (hhmm ? etToZone(hhmm) : null);
  const day = etDayLabel(status.next_entry_at);   // "Mon" when not today (ET), else null

  const dd = Math.floor(remaining / 86400);
  const h = Math.floor((remaining % 86400) / 3600);
  const mm = Math.floor((remaining % 3600) / 60);
  const ss = remaining % 60;
  const hms = `${h}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
  const countdown = dd >= 1
    ? `${dd}d ${hms}`
    : h >= 1 ? hms : `${mm}:${String(ss).padStart(2, "0")}`;

  return (
    <div className="next-entry" data-testid="next-entry">
      Next entry {day ? `${day} ` : ""}{hhmm} ET{local ? ` (≈ ${local} local)` : ""} — in {countdown}
    </div>
  );
}
