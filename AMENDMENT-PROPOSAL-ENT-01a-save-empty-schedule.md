# Proposed spec amendment (ENT-01a): allow SAVING an empty schedule; keep ARMING it rejected

**Date:** 2026-07-28
**Reported by:** Rick's side (production `live_app` panel, account 5WY11926)
**Repo state:** `main` @ `6dc5c99`, spec v2.07
**Type:** proposed amendment / save-path defect — routed to Ash per the repo contract (never improvise around a spec rule)

## Symptom

On **SCHEDULE & PARAMETERS**, deleting scheduled entries and clicking **Save** will not clear the *last* row. You can delete rows down to one, but removing the final row (leaving an empty schedule) never persists — the last row reappears immediately, and on refresh. Reproduced live: config walked **v17 → v20** as three rows were deleted successfully; the final row (`id=8`, 12:11) cannot be removed.

## Root cause — not a storage bug

`POST /schedule` validates *before* persisting and writes nothing on any error ([app.py:1063](backend/src/meic/adapters/api/app.py#L1063)). The validator rejects an **empty** schedule:

`backend/src/meic/domain/schedule.py:211`
```python
if not rows:  # ENT-01a: arming an empty schedule is rejected
    return [ScheduleError(field="entries", reason="empty_schedule")]
```

`ScheduleService.save` runs this same `validate_schedule` (via `self.validate`), so an empty POST returns `{"result": "invalid"}` → the endpoint **422s** → `state.entry_schedule` is never overwritten → the previously-persisted schedule reloads. The last row is therefore undeletable through the panel.

## Why this reads as an over-application of ENT-01a

The spec scopes the empty-rejection to **arming**, not to saving/persisting:

- **ENT-01a** (`spec/01-strategy-rules.md:31`): *"**Arming** requires ≥ 1 entry composed (**arming** an empty schedule is rejected) …"* — the rule is about the arm transition.
- **REC-07(5)** lists *"the standing entry schedule and all per-entry parameters"* as durable inventory. An empty schedule is a legitimate persisted state (a cleared draft), restored on boot like any other.

And arm-time is **already independently guarded**, so dropping the empty check from the *save* path does **not** weaken it — `backend/src/meic/application/preflight.py` `schedule_check()`:

```python
if not schedule_service._state.entry_schedule:
    return False, "arming an empty schedule is rejected (ENT-01a)"
```

Arm runs the full UC-02 pre-flight ([app.py:1077](backend/src/meic/adapters/api/app.py#L1077)) and this explicit check blocks arming an empty schedule regardless of what `validate_schedule` returns at save time. Net: an operator can never clear the schedule to a clean slate, even though (a) the spec only forbids *arming* empty and (b) arming is separately protected.

## Proposed change (for Ash to ratify)

Let **save** persist an empty schedule; keep **arm** rejecting it. One of:

1. **Move** the `if not rows → empty_schedule` rule out of `validate_schedule` (the shared save-path validator) into the arm/`may_arm`/preflight path only. Pre-flight already re-checks empty, so arm stays safe. *(Preferred — single source of truth: "empty is an arm-time failure, not a persistence failure.")*
2. Or have `ScheduleService.save` tolerate an empty list (treat "the only error is `empty_schedule`" as savable), leaving `validate_schedule` semantics intact for `may_arm`.

## Tests affected (flagged, not touched — spec/tests are owner-locked)

- `tests/domain/test_schedule.py:57 test_empty_schedule_is_rejected` — pins `validate_schedule([]) == {empty_schedule}`. Under option 1 this assertion re-homes to the arm/`may_arm` layer.
- **Must stay green** (arm safety, all go through `may_arm`/preflight, not save, so unaffected):
  - `tests/application/test_schedule_service.py:243 test_arming_an_empty_schedule_is_rejected`
  - `tests/adapters/test_api_schedule_and_fire.py:173`
  - `tests/bdd/test_tc_ent_07.py`, `tests/features/TC-ENT-07.feature` (TC-ENT-07)

## Interim operator guidance (no fix required for safety)

With the bot **DISARMED** (and **Stop-Trading ON**), a lingering schedule row is inert — nothing fires (ENT-03 requires ARMED ∧ Stop-Trading OFF ∧ Confirm-Live ON). The lone 12:11 row currently persisted is harmless until armed. Editing it to desired params is also an option instead of deleting.

## Reproduce

- **Panel:** delete all rows → Save → the last row returns.
- **API:**
  ```
  POST /schedule {"rows": []}
    → 422 {"result":"invalid","errors":[{"field":"entries","reason":"empty_schedule"}]}
  GET  /schedule
    → still returns the prior rows (nothing was persisted)
  ```
