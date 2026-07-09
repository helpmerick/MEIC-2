# Spec amendment proposal — local-time echo + military-time entry window

**To:** Ash (operator / spec owner)
**From:** coding agent
**Re:** `spec/` additions for schedule entry-time display and validation
**Status:** PROPOSED — awaiting ratification (I cannot edit the hash-locked `spec/`)

---

## Why

The operator sits in a different timezone from the exchange (e.g. London vs. ET).
The schedule is authored in **ET** (DAY-03), so a London operator entering `11:53`
has to do the ET↔local conversion — including DST — in their head. That is a
real-money footgun. Two related asks, plus a validation tightening:

1. Show, next to each ET entry time, its equivalent in the operator's **local**
   timezone, read live from the browser so DST is automatic.
2. Enforce **24-hour (military) HH:MM** entry times — no am/pm, no dotted `11.53`.
3. Operator ruling: an entry time is only valid **while the market is open**
   (09:30–16:00 ET). (This is the *value* of the time, independent of when the
   schedule is composed. The DAY-02 30-min-before-close buffer remains a separate,
   stricter gate on top.)

Ruling captured 2026-07-09 via the panel question: **"Within RTH session"** —
09:30–16:00 ET; pre-market/after-close times refused.

---

## Proposed spec text

### New — UI-23 (doc 03 / doc 07, UI rules): local-time echo for schedule times

> **UI-23 — Local-time echo.** Beneath each ET entry time, the schedule panel
> displays that time's equivalent in the operator's local timezone, resolved live
> from the browser (`Intl`), so daylight-saving transitions are handled
> automatically with no offset tables. **ET remains the system of record**
> (DAY-03); the local echo is display-only — never persisted, never sent to the
> broker, never a trading input (UI-03). When the entered time is not yet valid,
> the echo is replaced by the precise reason (not-military, or outside market
> hours).

### New — DAY-06 (doc 06 validation): entry times are 24-hour, within RTH

> **DAY-06 — Entry-time format & window.** A schedule entry time MUST be a
> 24-hour ("military") wall-clock time `HH:MM` in ET, hour `00`–`23`, minute
> `00`–`59` (leading zero on the hour optional). am/pm, dotted (`11.53`), compact
> 4-digit (`0930`), and out-of-range values are rejected per-row with reason
> `not_24h_military`. The time MUST fall within regular trading hours
> **09:30 ≤ t < 16:00 ET** (reason `outside_market_hours`); this is in addition
> to — not a replacement for — the DAY-02 `min_time_before_close` buffer, which
> continues to reject times too near the close (reason `too_close_to_close`). The
> backend is authoritative; any UI-side check is advisory.

*(`outside_market_hours` already exists in `validate_schedule`; DAY-06 formalises
it and adds the `not_24h_military` format gate.)*

---

## Proposed test cases

### TC-UI-05 — local-time echo (frontend)
- Given the operator's zone is `Europe/London`, an ET entry time of `11:53`
  displays `≈ 16:53 London` beneath the cell.
- The echo tracks DST automatically (same helper, correct offset per instant).
- A not-yet-valid time shows the reason instead of an echo (military / market-hours).

### TC-DAY-06 — entry-time format & window (backend)
- `11.53`, `1:53pm`, `0930`, `24:00`, `11:60`, `""` → `invalid`, per-row
  `time / not_24h_military`.
- `09:32`, `9:32`, `15:30`, `23:59` → pass the format gate.
- `08:00` (pre-market) and `16:30` (after close) → `invalid` (`outside_market_hours`).
- `09:30` (open edge) → saved.

---

## What is already built behind this proposal

The feature is implemented and green so it is ready the moment you ratify; nothing
here touches `spec/`:

- `frontend/src/time.ts` — DST-aware `etToZone`, `isMilitaryTime`,
  `withinMarketHours`, `zoneLabel` (browser `Intl`, no libraries).
- `frontend/src/components/SchedulePanel.tsx` — `<TimeHint>` under each time cell:
  local echo, or the precise military / market-hours reason.
- `backend/src/meic/application/schedule_service.py` — `_military_time_errors`
  gate before parse (`not_24h_military`); RTH already enforced by
  `validate_schedule` (`outside_market_hours`).
- Tests: `frontend/src/time.test.ts` (+ 3 panel tests), and
  `tests/application/test_schedule_service.py` (military + market-hours cases).
  Offline suite: **726 passed**; frontend **67 passed**; tsc clean.

**To ratify:** add UI-23 and DAY-06 to `spec/` with the wording above (adjust IDs/
wording as you see fit), regenerate `spec.lock.json` and the features, and I will
reconcile the code's rule-ID comments/test names to the ratified IDs.
