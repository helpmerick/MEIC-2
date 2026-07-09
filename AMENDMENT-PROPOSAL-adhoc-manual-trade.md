# Spec amendment proposal — ad-hoc manual trade with pre-fire simulation

**To:** Ash (operator / spec owner)
**From:** coding agent
**Re:** `spec/` additions: ENT-11 (ad-hoc manual entry) and UI-25 (manual-trade
card with Simulate)
**Status:** PROPOSED — awaiting ratification (implementation built behind it;
`spec/` untouched, hash-locked)

---

## Why (operator request, 2026-07-10)

The operator wants to fire a trade NOW with chosen parameters, without composing
a schedule row: "just like the existing entry conditions but without the time
target — hitting submit makes this instant." Plus a **Simulate** action showing
what strikes and premium the trade would get if fired at this moment, before
committing real money.

ENT-09 (manual fire) only fires an EXISTING schedule row. This adds an ad-hoc
lane that reuses the identical pipeline.

## Proposed spec text

### New — ENT-11 (doc 03, entry rules): ad-hoc manual entry

> **ENT-11 — Ad-hoc manual entry.** The operator MAY compose and fire an entry
> on demand with explicit parameters (contracts, target premium, wing width,
> stop settings, credit floors — every per-row field of doc 06 §37 EXCEPT the
> time). Rules:
>
> 1. **The identical pipeline (ENT-09 semantics).** An ad-hoc fire crosses the
>    same path as a scheduled entry: full ENT-03 gate chain, reconcile-block,
>    clock drift, RSK-08 order cap, RSK-04 max exposure, ENT-05
>    max_entries_per_day (it counts), STP-02c feasibility, the ORD-01/02/03
>    ladder, and on fill the STP-01 protect hand-off (stops, close targets, LEX,
>    EOD — indistinguishable from a scheduled entry thereafter). The ONLY rule
>    bypassed is the ENT-02 window, exactly as ENT-09 already rules for manual
>    presses (fresh intent by definition).
> 2. **Confirmation and idempotency as ENT-09/UI-22:** an OK dialog gates the
>    fire; a press_id makes a double-click one attempt; no confirmation = no
>    order and nothing recorded.
> 3. **A separate entry-number lane.** Ad-hoc entries are numbered from 101
>    upward per day (101, 102, ...). They must NEVER share numbers with schedule
>    rows (1..N): entry_ids key the ownership ledger, RSK-04 exposure book,
>    ORD-04 idempotency and the ENT-10 remaining-schedule filter — a collision
>    could block a scheduled entry or double-count exposure.
> 4. **Validation is the backend's** (UI-03): the same per-row range rules as a
>    schedule row (contracts 1–10, discrete stop set, width steps, per_side
>    rejected), minus the time rules.

### New — UI-25 (doc 03/07, UI rules): the manual-trade card with Simulate

> **UI-25 — Manual trade card.** A collapsible panel section ("Fire manual
> trade") holds the ENT-11 parameter form and two actions:
>
> - **Simulate trade** — read-only preview: runs LIVE strike selection against
>   the current chain exactly as a fire would (same selector, same row
>   parameters, same collision/credit gates) and displays the strikes it would
>   choose (short/long per side), each side's mid premium, the expected net
>   credit, and the structural worst case — or the precise skip reason
>   (incomplete_chain, no_valid_strikes, insufficient credit...). A simulation
>   places NO order, appends NO event, and consumes nothing (ENT-05 unaffected).
>   The display is labelled an ESTIMATE: the real fire re-selects from fresh
>   data and may differ (v1.46 estimate-honesty precedent).
> - **Fire** — the ENT-11 fire behind the UI-22-style OK dialog, enabled only
>   while entries are enabled (ARMED ∧ Confirm Live ∧ not Stop Trading).

## Proposed test cases

### TC-ENT-11
- An ad-hoc fire with explicit parameters places the condor and rests both stops
  (the live-shaped harness path), recorded with initiator `manual_entry`.
- Ad-hoc entry numbers start at 101 and never collide with schedule rows; an
  ad-hoc fill does not block any scheduled entry (ENT-10 filter unaffected).
- The ENT-03 chain applies: disarmed/stop-trading/confirm-live-off refuse it.
- It counts toward ENT-05 max_entries_per_day.
- Double-press = one attempt; unconfirmed = nothing recorded.
- Simulate returns strikes + premium and appends NO event, places NO order.
- Simulate surfaces the selector's skip reason verbatim when selection fails.

### TC-UI-07
- The card collapses/expands; Fire is disabled while entries are disabled;
  Simulate works regardless of armed state (read-only).
- The simulation output is labelled an estimate.

## Implementation notes (built behind this proposal)

- `ManualEntry.simulate(row)` — runs the selector with the row's
  SelectionConfig; returns condor details or skip reason; touches no state.
- `ManualEntry.fire(...)` unchanged in semantics; the API layer builds a
  ResolvedEntry from the posted parameters (same resolve path as a schedule
  row, dummy time) and allocates the next number in the 101+ lane from today's
  events.
- Endpoints: `POST /manual/simulate` (auth-gated like all mutating calls even
  though read-only — it consumes broker/data budget) and `POST /manual/fire`.
- Frontend: collapsible `ManualTradeCard` with the row-minus-time fields,
  Simulate result table, and the existing fire-dialog pattern.
