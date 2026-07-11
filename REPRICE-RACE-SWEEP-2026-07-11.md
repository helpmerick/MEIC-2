# Reprice-Race Pattern Sweep — 2026-07-11

Closes the incident-#2 class across every call site that acts on a working
order (replace/cancel/re-submit) without re-confirming its terminal state
first. Method per site: identify the guard, add the minimal one where
missing (reusing `execute_entry._fill_matches`, `cancel_taxonomy`'s ORD-08
taxonomy, or `stop_fill_watch`'s leg matchers — never a new normalizer), and
back each LIVE-WIRED site with a live-shaped-harness test (fill latency +
reject-on-replace-after-fill + cancel-after-fill, via `tests/harness/live_broker.py`,
extended this sweep with `race_fill_on_cancel`/`race_fill_on_replace`/
`hide_from_working_orders`).

**Wiring map** (verified by grep, not assumed): `execute_entry`, `recover_long`,
`close_entry`, `protect_position`, `reconcile`(boot)/`reconcile_boot`, and
`stop_fill_watch` are live-wired (composition/live.py, paper.py, server.py).
`eod_sweep.EndOfDaySweep`, `manual_close.ManualClose`, `decay_watcher.DecayWatcher`,
and `watchdog.StopWatchdog` are **not** constructed anywhere outside their own
tests — confirmed unwired, same "pieces exist, nothing drives them" pattern
`stop_fill_watch.py`'s own docstring already flags for RSK-04/day-supervisor/TPF-TPT.

## Table

| # | Call site (file:line) | Mutating call | Guard (mechanism) | Test | Status |
|---|---|---|---|---|---|
| 1a | `execute_entry.py:255/266` | submit / replace (reprice ladder) | Pre-replace `_filled()` re-check (d3453c9, prior sweep) | `test_live_shaped_fill_places_and_confirms_both_stops` | GUARDED-TESTED |
| 1b | `execute_entry.py:266` (replace call itself) | `broker.replace()` | **NEW**: `try/except` around `replace()`; on exception, re-check `_filled()` before propagating — a race in the pre-check→replace gap is recorded as a fill, not raised | `test_replace_race_mid_ladder_is_recorded_filled_not_raised` | FIXED-THIS-SWEEP |
| 1c | `execute_entry.py:300` (cancel-at-floor) | `broker.cancel()` | **NEW**: post-cancel `_filled()` re-check — a fill landing inside the cancel() round trip is recorded as a fill, not `unfilled_at_floor` | `test_cancel_at_floor_racing_a_fill_is_recorded_filled_not_skipped` | FIXED-THIS-SWEEP |
| 2a | `recover_long.py:118/129` | submit / replace (LEX ladder) | Pre-replace `_filled()` re-check (6e70603, prior sweep) | `test_live_shaped_lex_fill_mid_rung_never_duplicates` | GUARDED-TESTED |
| 2b | `recover_long.py:129` (replace call itself) | `broker.replace()` | **NEW**: same try/except + post-exception `_filled()` re-check as 1b | `test_replace_race_mid_ladder_is_recorded_sold_not_raised` | FIXED-THIS-SWEEP |
| 3 | `close_entry.py:194` (`_replace_stop`) | `broker.replace()` | ORD-08 `ReplaceFilled`/`ReplaceTerminal` classification (pre-existing) **+ NEW**: after retries exhaust as "unclassifiable", re-check `fills_since`/`_fill_matches` for `stop_id` before declaring `STILL_RESTING` — the live adapter never raises `ReplaceFilled` (see adapter finding below), so this recheck is what actually catches a live race | `test_replace_races_fill_is_detected_via_fills_since_not_left_resting`, `test_replace_succeeds_when_no_race_is_in_play` | FIXED-THIS-SWEEP |
| 4 | `protect_position.py:180-197` (`_place_and_verify`) | `broker.submit()` (stop, with retry) | **NEW**: on any retry (attempt > 0), scan `working_orders()` for an already-resting buy-to-close on this leg's symbol (`_find_resting`, reusing `stop_fill_watch`'s leg matchers) and **adopt** instead of resubmitting — tastytrade enforces no server-side idempotency key | `test_stop_confirmation_miss_adopts_resting_stop_instead_of_resubmitting` | FIXED-THIS-SWEEP |
| 5 | `reconcile.py:137` (boot stale-entry cancel) | `broker.cancel()` | **NEW**: post-cancel `fills_since`/`_fill_matches` check; a race appends `ReconciliationMismatch` (RSK-03 blocks new entries) rather than silently discarding a live, unprotected fill — never reconstructs the entry (ORD-09: no strikes known here) | `test_boot_cancel_race_on_stale_entry_order_is_never_silently_discarded`, `test_boot_cancel_with_no_race_never_raises_a_false_mismatch` | FIXED-THIS-SWEEP |
| 6 | `eod_sweep.py:66` | `broker.cancel()` | **NEW**: post-cancel `fills_since`/`_fill_matches` check distinguishes a raced fill from a clean cancel; raced fills go to a new `SweepResult.raced_fills` list + a distinct critical alert, never silently folded into `cancelled` | `test_stop_filling_during_eod_cancel_is_flagged_not_reported_clean`, `test_clean_cancel_with_no_race_reports_cancelled_as_before` | FIXED-THIS-SWEEP (not wired live — see note) |
| 7 | `manual_close.py:75` (`cancel_working`, CLS-03) | `broker.cancel()` | **NEW**: post-cancel `fills_since`/`_fill_matches` check; a race appends `ReconciliationMismatch`, alerts critically, and returns a distinct `CloseResult("race_detected", ...)` instead of `"cancelled"` | `test_cancel_working_racing_a_fill_is_flagged_not_reported_clean` | FIXED-THIS-SWEEP (not wired live — see note) |
| 8a | `decay_watcher.py:87` (`buyback`) | `broker.cancel()` | Pre-existing `cancel.get("status") == "FILLED"` check **+ NEW**: `fills_since`/`_fill_matches` recheck — the pre-existing check only matches `SimulatedBroker`'s shape; the live adapter's `cancel()` never carries a `"status"` key at all | `test_buyback_cancel_race_against_live_shape_is_still_caught`, `test_buyback_with_no_race_proceeds_normally` | FIXED-THIS-SWEEP (not wired live — see note) |
| 8b | `decay_watcher.py:138` (`reinflation_guard`) | `broker.cancel()` + `broker.submit()` (re-protect) | **NEW**: `fills_since`/`_fill_matches` check on `buyback_id` before cancelling/re-protecting — a buyback that already filled must not get a phantom stop rested on its now-flat leg | `test_reinflation_guard_never_reprotects_a_leg_the_buyback_already_closed` | FIXED-THIS-SWEEP (not wired live — see note) |
| 9a | `watchdog.py:96` (`escalate`, pre-submit check) | `broker.working_orders()` read | `_resting_stop_filled` pre-check (pre-existing) — **BONUS FIX**: it matched only `.order_id`, so it always misreported "filled" against any live/SDK-shaped order (`.id` only), the exact `.order_id`-vs-`.id` class this whole sweep is about. Fixed to match both shapes | `test_resting_stop_fills_between_precheck_and_submit_is_alerted_not_silent` (would have failed to even reach the intended scenario without this fix — see write-up) | FIXED-THIS-SWEEP (not wired live — see note) |
| 9b | `watchdog.py:121` (`escalate`, post-submit cancel) | `broker.cancel(resting_id)` | **NEW**: `fills_since`/`_fill_matches` check on `resting_id` between the escalation's own submit and its cancel — the broker cannot un-submit the escalation's buy, so a genuine double-fill is alerted loudly instead of silently treated as one clean escalation | `test_resting_stop_fills_between_precheck_and_submit_is_alerted_not_silent`, `test_escalation_with_no_race_cancels_the_resting_stop_as_before` | FIXED-THIS-SWEEP (not wired live — see note) |
| 10 | `live_wiring.py:122/126` (`CountingBroker`) | pass-through `submit`/`replace` | Confirmed pure pass-through (`__getattr__` delegates everything else; `submit`/`replace` only add `cap.record()`) — preserves every guard above unchanged | `tests/composition/test_live_wiring.py::test_the_counting_broker_passes_everything_else_through` (pre-existing) | GUARDED-TESTED |
| 11a | `adapters/sim/simulated_broker.py:222-235` (`replace`) | classify-then-cancel-then-submit | Classifies the OLD order's status (WORKING/FILLED/terminal) **before** ever touching cancel/submit — raises `ReplaceFilled`/`ReplaceTerminal` per ORD-08 | Existing suite (`test_close_entry_live_shaped.py` proves the CLS-01 caller side against the *live*-shaped analogue) | GUARDED-TESTED |
| 11b | `adapters/tastytrade/adapter.py:204-275` (`replace`/`_replace_fallback`) | native replace, or cancel-then-submit fallback | Fallback probes `cancel()` **before** ever submitting a second order (never double-buys) | N/A — cert-only, `pytest -m contract` | **GAP-FLAGGED** (pre-existing, already documented in the adapter's own docstring — see Operator Notes) |
| 11c | `tests/harness/live_broker.py` (`LiveShapedBroker`) | test harness | **NEW**: `cancel()` now models cancel-after-fill (ambiguous error, not a false "cancelled"); added `race_fill_on_cancel`, `race_fill_on_replace`, `hide_from_working_orders` hooks so every site above has a live-shaped test | Used by every new test in this sweep | FIXED-THIS-SWEEP |

## Operator notes — flagged, not improvised on

1. **`adapters/tastytrade/adapter.py`'s `replace()`/`_replace_fallback` residual gap (item 11b) is pre-existing and already documented in the adapter's own docstring** — it does not yet raise `ReplaceFilled` for a genuine ORD-08a race against cert; the exact error shape cert returns for "already filled" vs "already gone" vs "rate limited" is unverified (assumption 5). I did **not** guess at cert's error strings. Instead, `close_entry.py`'s `_replace_stop` (item 3) now re-checks `fills_since`/`fill_legs` directly after the ORD-08 classification loop exhausts, which closes the *practical* impact (a live replace-race is now correctly recorded as a fill, not a misleading "left resting" alert) without touching the adapter. The adapter-level fix (raising `ReplaceFilled` from a classified cancel-failure payload) still needs `pytest -m contract` characterization before it can be written safely — this is the pre-existing, already-escalated item, re-confirmed still open.

2. **`eod_sweep.EndOfDaySweep`, `manual_close.ManualClose`, `decay_watcher.DecayWatcher`, and `watchdog.StopWatchdog` are not wired into the live or paper composition anywhere** (verified by grep — no `EndOfDaySweep(`, `ManualClose(`, `DecayWatcher(`, or `StopWatchdog(` outside their own modules and unit tests). This means:
   - EOD-03's "confirm zero working orders at day end" gate never actually runs live today.
   - The operator's Close button on a WORKING (pre-fill) entry has no live path at all (`panel_commands.py` only closes *filled* entries via `close_as`/`CloseEntry`).
   - Decay buyback and the STP-03b secondary watchdog are both dormant.

   This is the same "pieces exist, nothing drives them" pattern `stop_fill_watch.py`'s own docstring already lists (RSK-04, day-supervisor auto-restart, TPF/TPT). It is out of this sweep's scope to wire them — the task was to guard the pattern preventatively so wiring any of them in later cannot resurrect incident #2's class, which is what items 6-9 do. **Flagging this wiring gap itself for the operator's prioritization — EOD-03 in particular seems like it should be running today.**

3. **Bonus finding, not asked for but in-scope**: `watchdog.py`'s `_resting_stop_filled` (item 9a) matched only `.order_id`, which is the *exact* `.order_id`-vs-`.id` shape bug this whole incident class is named for (the same bug `protect_position._confirmed_qty` was fixed for on 2026-07-09). Against any live/SDK-shaped broker this made the pre-check **always** report "already filled," aborting every escalation before it could ever submit. Fixed alongside item 9b since a live-shaped test could not otherwise reach the scenario it needed to prove.

## Full-suite verification

```
.venv\Scripts\python.exe -m pytest -q -m "not contract"
  1132 passed, 13 deselected   (baseline 1116 + 16 new tests: 2+2+2+2+1+3+2+2 across
                                 test_live_fill_path.py, test_recover_long_live_fill_path.py,
                                 test_close_entry_live_shaped.py [new],
                                 test_reconcile.py, test_eod_sweep_live_shaped.py [new],
                                 test_tc_cls_02.py, test_decay_watcher_live_shaped.py [new],
                                 test_watchdog_live_shaped.py [new])

.venv\Scripts\python.exe scripts/verify_spec_lock.py
  spec lock verified: 17 files intact.

.venv\Scripts\python.exe scripts/check_traceability.py
  traceability ok: 216 rules covered, 147 test cases implemented or feature-backed.
```

`spec/`, `spec.lock.json`, `tests/features/`, `scripts/`, and `.github/` were not touched.
Three pre-existing broker test-doubles (`tests/application/test_tc_eod_03.py::SweepBroker`,
`tests/bdd/test_tc_own_11.py::_Broker`, `tests/application/test_tc_cls_02.py::RecordingBroker`)
gained a `fills_since` stub to complete their `BrokerGateway` shape — no assertions changed.
