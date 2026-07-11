# Spec amendment proposal — exchange calendar for the supervisor, gates, and countdown

**To:** Ash (operator / spec owner)
**From:** coding agent
**Re:** DAY-01/DAY-02 wiring + a UI-24 weekend/holiday rollover
**Status:** RATIFIED in session — operator ruling 2026-07-11 (this document records it; `spec/` is hash-locked)

---

## Why

On Saturday 2026-07-11 the panel showed **"Next entry 11:56 ET — in 7:03:05"**.
No entry can fire on a Saturday. Three gaps, found while fixing it:

1. `/day/status` (UI-24) composed schedule rows for *today's calendar date*
   whatever day that was — weekends and holidays included.
2. The ENT-10 day supervisor started a day task on closed days too; each entry
   was then individually refused by the at-fire-time ENT-03 market-open gate,
   writing `EntrySkipped` noise into the event log every weekend.
3. `LiveMarketGates` was constructed with its **default (empty) holiday set** —
   DAY-01 says the bot "MUST consult an exchange calendar (including half
   days)", but market holidays looked like open days to the gate. (Weekends
   were already caught by the weekday check.)

## Ruling (captured 2026-07-11, in-session)

1. **Weekend/holiday countdown rolls forward.** On a NON-trading day, UI-24
   shows the *next trading day's* first entry, labelled with its day and a
   day-aware countdown: `Next entry Mon 11:56 ET (≈ 16:56 London) — in 2d 2:03:05`.
   On a trading day nothing changes: an exhausted schedule still reads
   **"no more entries today"** (TC-UI-06's locked wording is untouched — the
   rollover activates only on days the exchange is closed).
2. **Holidays are computed, not configured.** NYSE full-day holidays and
   early-close half-days are exchange facts derived algorithmically for any
   year (`backend/src/meic/application/nyse_holidays.py`): the ten standard
   holidays with Saturday→Friday / Sunday→Monday observance (New Year's on a
   Saturday not observed at all, matching 2021-12-31), Good Friday via the
   computus; half-days are July 3rd / Christmas Eve when Monday–Thursday and
   the day after Thanksgiving. Pinned against published calendars in
   `tests/application/test_nyse_calendar.py`.
3. **The supervisor consults the calendar before scheduling** (DAY-01's own
   words): no day task at all on a closed day. The at-fire-time ENT-03 gate
   remains as the safety net. Manual `/day/start` is left as-is (explicit
   operator action; entries still cross the full gate chain).

## Implementation (same session)

- `market_calendar.next_trading_day` — the forward walk (DAY-01).
- `nyse_holidays.py` — holiday/half-day rules; `holidays_near`/`half_days_near`
  windows for scans.
- `server.py` — `_next_trading_day_extras` + calendar checks in `/day/status`
  and `_supervise_once`; `LiveMarketGates` now wired with a decade of
  holidays/half-days at boot.
- `NextEntryCountdown.tsx` / `time.ts` — day label + `Nd H:MM:SS` countdown;
  the ET/local times now convert **the instant** rather than re-reading an
  HH:MM as "today", which also fixes a latent wrong-offset bug when the next
  entry sits across a DST switch.
- Tests: `test_nyse_calendar.py`, `test_day_calendar_gate.py`,
  `NextEntryCountdown.test.tsx` (rollover case added; the three TC-UI-06-bound
  test names unchanged and passing).

## Proposed spec text (for ratification into doc 03 §UI-24, next spec release)

> **UI-24a — Closed-day rollover.** On a day the exchange calendar (DAY-01)
> marks closed, the next-entry countdown shows the next trading day's first
> scheduled entry — ET time prefixed with the ET weekday, local echo per
> UI-23, and a countdown spanning the closed days (`2d 2:03:05`). "No more
> entries today" remains the exhausted-schedule state on trading days only.
> The supervisor starts no day task on closed days; ENT-03's market-open gate
> is unchanged and remains authoritative at fire time.
