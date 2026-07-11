# HANDOFF — session state for a fresh start

Last updated: 2026-07-11 late — CONSTRUCTION CLOSED at `0e5cac8` (spec
v1.61 implemented; EOD-03 sweep + CLS-03 working-entry cancel wired,
fail-first). Next phase: VERIFICATION MODE per the adviser's graduation
criteria. Baselines now: backend 1214 / frontend 211 / lock 17 / trace
218 rules-150 TCs, all three v1.61 TCs executing. Deployed: :8010 runs
HEAD; panel index served no-cache (deploys visible on plain refresh);
stale :8000 servers killed. Escalations awaiting adviser: UI-23a label
("local" shipped vs master's city label), STP-08a defer-vs-LEX-05
fallback on present-but-unusable quotes, "one LEX ladder ever" vs REC-03
resume reading, /day/status seconds_to_next naive-wall-clock DST gap
(one-line fix, needs ruling), LEX order-id journaling for EOD-03 sweep
scope, RPT-16 ratification (text pasted 2026-07-11). Older sections
below describe the mid-construction state and remain for context.
Read this + `CLAUDE.md` + `spec/README.md` before starting any new slice.
Per operator instruction, each new slice starts in a FRESH session off this
file + the spec. (The original day-one kickoff prompt this file used to hold
is preserved in git history.)

## Where things stand

- **Branch**: `live-testing-2026-07-09`, fully pushed. HEAD `2d90f52`.
- **Deployed**: live_app on `127.0.0.1:8010` running HEAD (uvicorn,
  `meic.adapters.api.server:live_app --factory --app-dir backend/src`;
  frontend served from `frontend/dist`, rebuild with `npm run build` when
  frontend changes). Port 8010 always — 8000 is held by Docker/stale.
- **Baselines**: backend `pytest -q -m "not contract"` = **1132 passed**
  (contract tests need RTH + sandbox); frontend **139 vitest + tsc clean**;
  spec lock **17/17**; traceability **216 rules / 147 TCs**.
  Always use `.venv\Scripts\python.exe` (global python lacks plugins).
- **Spec**: v1.59 on main. spec/ is READ-ONLY (hash-locked).
- **Operator state**: Stop Trading ON (operator will run the supervised
  outage drill and decide separately). Live position 2026-07-10#1 fully
  expired; its C7565 stop-out was caught up into the journal by the
  detection tick (fill 4.80, slippage −0.10).

## Recent slices (all shipped, do NOT rebuild)

| Slice | Commit |
|---|---|
| v1.59 settlement broker-journaling | `057a4cf` |
| TPF+TPT exits slice | `98e56a5` |
| F1 LEX ladder fix + live stop-fill detection (EC-STP-06 every tick) | `6e70603` |
| v1.55 STK-10 baseline + v1.56 drill semantics + v1.57 manual floors | `8cd8c5e` |
| ENT-08 warm-up wiring + near-trigger ruling + floor dropdowns | `ef9b079` |
| Reprice-race pattern sweep (11 sites, table report) | `2d90f52` |

Key reports at repo root: `REPRICE-RACE-SWEEP-2026-07-11.md`,
`SECURITY-REVIEW-2026-07-10.md`, `CODEBASE-HEALTH-2026-07-10.md`,
`LIVE-TEST-LOG-2026-07-09.md`, `AMENDMENT-PROPOSAL-*.md`.

## Open queue (in priority order)

1. **Unwired-live components** (7th instance of the exists-but-unwired
   class, flagged in the sweep report Operator Notes): `EndOfDaySweep`
   (EOD-03 zero-working-orders gate never runs live), `ManualClose`
   (no live Close path for a WORKING pre-fill entry), `DecayWatcher`,
   `StopWatchdog` (STP-03b). All four are now race-guarded so wiring them
   cannot resurrect incident #2. Needs operator prioritization/ratified
   order before wiring.
2. **Cert contract suite** (`pytest -m contract`) on operator go — needs
   RTH. Includes characterizing the adapter's replace-after-fill error
   shapes so `TastytradeAdapter.replace()` can raise ORD-08a
   `ReplaceFilled` properly (pre-existing gap, practically mitigated in
   close_entry via fills_since recheck).
3. **Ratification queue awaiting adviser**: RPT-16 spec pin (proposal at
   repo root); near-trigger formula spec pin (ruled 2026-07-11, built);
   settlement string-comparison hardening; exits flags (paper mark stream,
   UI-15 revalidation, tp_gap_pct wiring); side-scoped flatten
   (flatten_side honesty gap, see protect_position `_go_unprotected`
   docstring); "unprotected" initiator ratification; ENT-08.1 proactive
   token renewal (SDK exposes no expiry reader — scope boundary).
4. **Known latent flags** (documented in code, not wired live): watchdog
   journals ShortStopped before buy-back confirms; decay buyback fill vs
   symbol-fallback misread window; `OwnershipLedger` never populated in
   production; RPT day-report floor columns not rendered.

## Process contract (operator-ratified)

- Lead plans/reviews on the strongest model; workers implement (Sonnet for
  logic, Haiku for mechanical). Light process (one worker + lead review)
  for small/medium; full loop (Sonnet review rounds + ONE Opus final) for
  big/safety-critical slices. Fail-first proof standard for any
  exists-but-unwired fix: widen the rail capstone in
  `tests/application/test_live_app.py`, show it failing, then wire.
- CLS-02: the agent NEVER places ad-hoc broker orders — closes go through
  CloseEntry or the operator.
- Race-guard convention: any code mutating a working order re-confirms
  terminal state first (`execute_entry._fill_matches`, ORD-08 taxonomy, or
  stop_fill_watch leg matchers — never a new normalizer), backed by a
  `tests/harness/live_broker.py` live-shaped test.
- Spec ambiguity → STOP, write an amendment proposal for the operator.
- Deploy = commit → push → (frontend build if needed) → restart :8010 →
  verify `/state` returns 200. Never commit
  `tests/contract/observations/*.json` churn with feature work.
