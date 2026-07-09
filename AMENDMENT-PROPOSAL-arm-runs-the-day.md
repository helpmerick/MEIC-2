# Spec amendment proposal — Arm runs the trading day + next-entry countdown

**To:** Ash (operator / spec owner)
**From:** coding agent
**Re:** `spec/` additions: ENT-10 (arming starts the scheduled day) and UI-24
(next-entry countdown)
**Status:** PROPOSED — awaiting ratification (implementation built behind it;
`spec/` untouched, hash-locked)

---

## Why

Live-test finding, 2026-07-09 (operator-flagged, audit log outstanding item 2):
**arming did not cause scheduled entries to fire.** `Arm` only flipped a state
flag; the wall-clock loop that watches the schedule (`run_day`) started only via
a `/day/start` endpoint that no UI element and no automation ever called. The
operator armed, confirmed live, composed a 12:35 entry — and 12:35 passed with
nothing watching. "ARMED" meant *entries are permitted*, not *entries will
happen*. That contradicts what UC-02 already implies and what the operator
reasonably expects.

Secondly, the operator asked for visible evidence that the schedule is being
watched: a **countdown to the next entry**.

---

## Proposed spec text

### New — ENT-10 (doc 03, entry rules): arming runs the trading day

> **ENT-10 — Arm runs the day.** Entering the ARMED state MUST start the
> scheduled trading day: a runtime task that watches the wall clock and attempts
> each composed entry at its time (UC-01/UC-02). No separate "start" action may
> be required. Specifically:
>
> 1. **Arm ⇒ watching.** On a successful Arm (pre-flight passed), the day task
>    starts for the REMAINING schedule — entries whose time is still in the
>    future. Entries whose time already passed are not attempted (they are
>    simply absent from this run; ENT-02's missed-window rule remains the guard
>    for borderline cases).
> 2. **Boot restore (REC-07).** If the bot boots with persisted state ARMED, the
>    day task starts automatically for the remaining schedule. A restart must
>    not silently turn a watching bot into an inert one.
> 3. **Disarm / kill ⇒ not watching.** Disarm stops future entries immediately;
>    an in-flight entry attempt is ATOMIC — it completes (fill→protected, or
>    cancelled-at-floor→skip) and is never abandoned mid-flight, which would
>    orphan a resting order at the broker. Stop Trading continues to refuse
>    entries via RSK-01 whether or not the task runs.
> 4. **Entry identity is stable.** A day task started mid-day (re-arm, restart)
>    MUST attempt entries under their ORIGINAL schedule numbers — filtering the
>    remaining schedule must never renumber entries, or their entry_ids would
>    collide with already-filled ones (ORD-04 idempotency, RSK-04 exposure book).
> 5. **Every rail still applies.** The day task bypasses nothing: each entry
>    crosses the full ENT-03 chain, RSK-04/08, STP-02c at its fire time. An
>    entry already attempted today (filled or skipped) is never re-attempted.
> 6. **A crashed day task is an alert, not a retry loop.** If the task dies with
>    an error while ARMED, raise a critical alert (RSK-06) and do NOT auto-
>    restart until the operator cycles Disarm→Arm (prevents a tight crash loop
>    placing orders).

### New — UI-24 (doc 03/07, UI rules): next-entry countdown

> **UI-24 — Next-entry countdown.** While ARMED with entries remaining, the
> panel MUST show the next scheduled entry (its ET time, and its operator-local
> equivalent per UI-23) and a live countdown until it fires. States:
>
> - DISARMED → "schedule idle — arm to run" (no countdown).
> - ARMED, entries remaining → "next entry HH:MM ET (≈ HH:MM local) — in MM:SS"
>   ticking each second.
> - ARMED, none remaining → "no more entries today".
>
> The countdown is DISPLAY-ONLY (UI-03): the authoritative next-entry time and
> remaining seconds come from the backend (`/day/status`); the client only
> animates between polls. The backend clock (DAY-03-verified) is the source of
> truth, never the browser clock.

---

## Proposed test cases

### TC-ENT-10 — arm runs the day
- Arming with a future entry starts the day task; the entry fires at its time
  through the full gate chain (proved with the live-shaped broker harness).
- Booting with persisted ARMED state starts the task (REC-07 restore).
- Disarming cancels the task; re-arming starts a fresh one for the remainder.
- A mid-day (re)start attempts only future entries, under their ORIGINAL entry
  numbers; already-attempted entries are not re-attempted.
- A day task that dies with an error raises a critical alert and is not
  auto-restarted.

### TC-UI-06 — countdown
- Armed with a next entry → the panel shows its ET time and a ticking countdown.
- Disarmed → "schedule idle"; armed with none left → "no more entries today".
- The countdown value derives from the backend's `seconds_to_next`, not the
  browser's clock.

---

## Implementation (built behind this proposal; adjust IDs on ratification)

- `live_app`: a day **supervisor** loop — observes `state.armed`; starts
  `run_day` with the remaining, originally-numbered rows; cancels on disarm;
  alert-once on crash. Boot-restore falls out of the same loop.
- `ScheduledRow.number` + `run_day` honouring it (identity-stable filtering).
- `/day/status` extended: `armed`, `next_entry_at`, `seconds_to_next`,
  `entries_remaining`.
- Frontend: `NextEntryCountdown` (polls `/day/status`, ticks locally, reuses the
  UI-23 local-time echo).

---

## Open ruling requested (final review, 2026-07-09)

**Mid-day schedule reorder/delete breaks positional number stability.** Entry
numbers are positional (saved row *i* → entry #*i*). If the operator deletes a
FIRED row mid-day, the remaining rows shift position: a still-pending entry can
inherit the filled entry's number, and `_remaining_rows` will then treat it as
already attempted and silently drop it. Two candidate rulings — operator to pick:

1. **Block schedule saves while ARMED** — simplest; editing requires a disarm.
2. **Durable per-row entry ids** — assigned at save, stable across later edits.

Until ruled, avoid editing the schedule mid-day while armed.
