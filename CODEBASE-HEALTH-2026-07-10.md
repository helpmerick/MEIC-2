# MEIC Bot — Codebase Health & Redundancy Audit — 2026-07-10

Scope: `backend/src/meic/` (11,747 LOC), `frontend/src/` (4,946 LOC incl. tests),
`tests/` (19,112 LOC). `spec/` excluded (read-only, not audited). All findings
are evidence-based (file:line); every recommendation is labeled:

- **SAFE-NOW** — mechanical, no behavior change, low risk
- **NEEDS-TESTS** — behavior-preserving but touches enough surface to want new/adjusted tests first
- **NEEDS-RATIFICATION** — touches spec-adjacent behavior; requires operator sign-off before any change

## Baseline

```
pytest -q -m "not contract" -p no:cacheprovider   (via .venv, system Python lacked pytest_bdd)
976 passed, 13 deselected, 222 warnings in 14.51-14.95s
```
Green. The 13 deselected tests are the `@pytest.mark.contract` sandbox tests in
`tests/contract/` (4 files: `test_dxlink_adapter_wiring.py` (2),
`test_tastytrade_adapter_wiring.py` (5), `test_tc_stp_13.py` (5),
`test_live_selection_cert.py` (1) — last touched in the `bc271af` /
`527906d` commits, i.e. currently well-maintained, not rotting yet).

Slowest test: `tests/bdd/test_tc_ui_05.py` at 4.37s (~29% of total suite time)
— by design: it shells out to `npx vitest run` once per session (see
`tests/bdd/conftest.py`'s `vitest_result` fixture) to execute the *real*
`frontend/src/time.test.ts` rather than reimplement ET/DST math in Python.
This is a deliberate, well-documented cross-stack check, not redundant work —
flagged here only as an environment coupling: if Node/vitest is slow or
missing in some future CI runner, this one test dominates wall time and could
mask itself as a hang.

---

## Executive summary — top 10 actions, ranked by value/risk

1. **[SAFE-NOW] Extract the byte-for-byte-identical logic in
   `composition/paper.py` / `composition/live.py`** — `_NullAlerts` (3 lines),
   `_auto_flatten_entry` (45 lines incl. docstring), `_shorts` (14 lines) are
   verbatim duplicates; `_on_filled` is duplicate except for a dead stray
   docstring-as-statement in paper.py that live.py lacks (drift already
   happening — see §1.1). Move the shared ~65 lines into one mixin/module;
   the two composition roots keep only what actually differs (broker/feed
   construction). Highest value: this is the file pair most likely to be
   hand-edited under incident pressure, and it already has one instance of a
   copy that didn't make it to both sides.

2. **[SAFE-NOW] Consolidate the duplicate `_order_id()` broker-shape
   normalizer** in `application/eod_sweep.py:14-21` and
   `application/reconcile_boot.py:59-65` — identical function, same
   attr-then-dict fallback. The `_fill_matches` pattern in
   `execute_entry.py:32-45` is the same *shape* of fix for the exact
   2026-07-09 incident the docstring names ("a live fill object has no
   `.get`"). Three independent reimplementations of "broker orders are
   sometimes dicts, sometimes SDK objects" is exactly the failure mode that
   caused that incident — the next call site will likely reinvent it a
   fourth time instead of finding the other three.

3. **[SAFE-NOW] Consolidate test helpers `_jwt`/`_isolate_env`/`_cert_env`**
   into a shared `tests/harness/` module or root `tests/conftest.py`.
   `_isolate_env` and `_cert_env` are already self-aware duplicates (their
   own docstrings say "Same isolation as test_live_app.py") — they've simply
   never been finished. `_jwt` has ALREADY DIVERGED in one of its 7 copies
   (see §1.4) — the dangerous kind.

4. **[SAFE-NOW / NEEDS-TESTS] Split `adapters/api/server.py` (933 LOC, the
   largest file in the repo).** It mixes: env/config parsing (10 pure
   functions), two FastAPI app factories, day-supervisor orchestration logic
   that `tests/application/test_day_supervisor.py` imports directly from the
   adapter module (a layering tell — see §3.1), a broker read facade, and
   live P/L enrichment. Recommend extracting the day-supervisor helpers into
   an `application/` module and the env/config helpers into their own file,
   leaving `server.py` as FastAPI route registration + composition wiring.

5. **[SAFE-NOW] Remove or genuinely wire `ChainSnapshot.put_band`/`call_band`**
   (`adapters/dxlink/chain_snapshot.py:44-45`) — the module's own docstring
   says they're kept "for the live P/L card and contract-test visibility,"
   but grep confirms zero production reads outside the defining module; the
   live P/L card (`server.py`'s `_live_pnl_enricher`/`_sample_marks_once`)
   never touches them. The claim in the comment is stale. Only 4 test files
   construct/assert on them directly (`test_entry_mark_sampler.py`,
   `test_live_selection.py`, `test_live_pnl_enricher.py`,
   `test_live_selection_cert.py`) — real product code reads nothing.

6. **[NEEDS-TESTS] Consolidate the 3 near-duplicate `FastClock`/`_Clock` test
   doubles** (`tests/application/test_live_runtime.py:24`,
   `tests/composition/test_live_runtime_numbers.py:23` — the second literally
   comments "see test_live_runtime.py"; and the no-op-`wait_until` `_Clock` in
   `tests/application/test_manual_fire_shield.py:27` /
   `tests/bdd/test_tc_ent_08.py:51`) into `tests/harness/fake_clock.py`
   alongside the existing `FakeClock`. Only 3 of ~10 clock-double
   definitions in the test suite actually import the shared harness.

7. **[NEEDS-RATIFICATION] Centralize the pre-v1.53 durable-id fallback shim**
   scattered across 5 files (`adapters/api/app.py:425`,
   `application/schedule_service.py:85,225-226`,
   `composition/live_runtime.py:83`, `composition/live_wiring.py:148-157`).
   Functionally consistent everywhere it was checked, but the "row has no
   `id` key yet, fall back to position" logic is reimplemented rather than
   shared, and it's explicitly transitional (retires once every operator
   schedule has been resaved since v1.53). Centralizing the shim now is
   SAFE-NOW; deciding *when* it's safe to delete entirely is an operator call
   (depends on whether any pre-v1.53 schedule is still live) — hence
   NEEDS-RATIFICATION for the removal, SAFE-NOW for consolidating it into one
   helper meanwhile.

8. **[SAFE-NOW] Harmonize the two `_drive()` polling helpers** in
   `tests/application/test_entry_pipeline.py:25-36` (2000 iters × 5s advance
   = 10,000s simulated budget) and `tests/application/test_live_fill_path.py:45-55`
   (3000 iters × 1s = 3,000s budget) — same technique, different constants,
   no shared home. Move to `tests/harness/` with a `max_wait_s` parameter.

9. **[NEEDS-RATIFICATION] Resolve the `_auto_flatten_entry` OPEN ITEM**
   documented identically in both `paper.py:82-101` and `live.py:107-126`:
   `config.unprotected_action`'s `flatten_side` setting is not honored (both
   settings currently produce a whole-entry close). This is flagged in the
   code as reported-not-resolved; it is duplicated by virtue of finding #1,
   so fixing it once (after the composition-root merge in #1) fixes it in
   both modes atomically instead of needing two synchronized edits.

10. **[SAFE-NOW] Fix the `_on_filled` drift** in `composition/paper.py:132`
    — a stray string literal sitting where a docstring should be (dead
    no-op statement) that `live.py`'s otherwise-identical `_on_filled` does
    not have. Harmless today, but it is live *proof* that these two
    functions are already drifting under copy-paste maintenance — the
    clearest argument for #1.

---

## 1. Duplication

### 1.1 `composition/paper.py` vs `composition/live.py` — near-twin composition roots

`diff -u backend/src/meic/composition/paper.py backend/src/meic/composition/live.py`
shows the files differ only in: docstrings, imports, the broker/feed
construction in `__post_init__`, the extra `connect()` method on
`LiveComposition`, and `paper.py`'s extra `compose_and_arm()` (used only by
the demo runtime, `composition/runtime.py:65`).

Byte-for-byte identical blocks:
- `_NullAlerts` class — `paper.py:34-36` / `live.py:34-36`
- `_auto_flatten_entry` — `paper.py:68-112` / `live.py:93-137` (45 lines,
  including the full OPEN ITEM docstring, word for word)
- `_shorts` — `paper.py:114-127` / `live.py:139-152` (14 lines)
- `_on_filled` — `paper.py:129-148` / `live.py:154-171`, **except**:
  `paper.py:132` has a stray string-literal statement
  (`"""STP-01 hand-off: place the two resting stops..."""`) sitting after the
  first line of the function body — not a docstring (wrong position, has no
  effect), and **not present in `live.py`**. This is drift already in
  progress: whichever file was edited second didn't carry the same edit to
  the sibling.

**Divergence risk**: HIGH by construction — this is exactly the pattern the
module docstrings both warn about ("paper and live are structurally separate
wirings, not a flag") while defeating that intent by hand-copying the same
30-body logic into two files instead of sharing it.

**Consolidation**: extract `_NullAlerts`, `_shorts`, `_auto_flatten_entry`,
`_on_filled` into a shared base (e.g. a `_BaseComposition` mixin/dataclass in
`composition/base_composition.py`, or free functions taking `comp` as an
explicit argument) that both `PaperComposition` and `LiveComposition`
compose. Risk: LOW — no behavior change if done as a pure extract; the
existing `tests/application/test_compositions.py` and
`tests/composition/test_live_wiring.py` exercise both paths already and would
catch a slip.

### 1.2 Broker-shape normalizers (dict vs. SDK object)

Three independent reimplementations of "a broker order/fill is sometimes a
dict (fakes/paper), sometimes an SDK object (live)":

- `application/eod_sweep.py:14-21` — `_order_id(order)`: tries
  `getattr(order, "order_id"/"id")`, falls back to `dict.get`.
- `application/reconcile_boot.py:59-65` — `_order_id(o)`: **identical**
  implementation, different file.
- `application/reconcile_boot.py:48-58` — `_symbol_and_signed_qty(p)`: same
  dict-vs-getattr shape, different fields.
- `application/execute_entry.py:32-45` — `_fill_matches(fill, order_id)`:
  same shape again, and its docstring names the actual production incident
  this pattern exists to prevent: "a live fill object has no `.get` ... This
  is what left a filled condor unprotected on 2026-07-09."

**Divergence risk**: HIGH. The docstring in `execute_entry.py` documents that
getting this wrong once already caused a real incident. Having it
re-implemented three more times (once verbatim) means any new call site is
more likely to write a fourth, subtly different version than to find and
reuse the other three — the same failure mode recurring by omission rather
than by a typo.

**Consolidation**: add one shared accessor, e.g.
`def broker_attr(obj, *keys, default=None)` trying `getattr` then
`dict.get` for each key in order, in a small shared module (e.g.
`application/broker_shapes.py`, since this is pure normalization logic with
no I/O — legal in the application layer per doc 05). Replace the 3
`_order_id` occurrences and re-express `_fill_matches`/`_symbol_and_signed_qty`
on top of it. Risk: LOW-MEDIUM (NEEDS-TESTS) — behavior must stay identical
for both dict and object shapes; `tests/application/test_eod_sweep.py`,
`test_reconcile_boot.py`/`test_reconcile.py`, and `test_entry_pipeline.py`
already cover both shapes and should be run as the acceptance gate.

### 1.3 `_on_filled`/`_shorts` — see 1.1 (same finding, cross-referenced)

### 1.4 Test-helper duplication (`_jwt`, `_isolate_env`, `_cert_env`, `_drive`, clock doubles)

`_jwt(iss)` — builds a fake unsigned JWT with an `iss` claim — is defined
independently in **7 files**:
`tests/adapters/test_broker_intent_contract.py:45`,
`tests/application/test_day_supervisor.py:357`,
`tests/application/test_compositions.py:38`,
`tests/adapters/test_tastytrade_adapter.py:18`,
`tests/adapters/test_occ_and_acl.py:22`,
`tests/adapters/test_production_guard.py:30`,
`tests/application/test_live_app.py:26`,
`tests/bdd/test_tc_ent_10.py:164`.

Six are identical:
```python
def seg(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"
```
**One has already diverged** — `tests/adapters/test_tastytrade_adapter.py:18-21`
adds an extra claim: `seg({'iss': iss, 'scope': 'read trade'})`. This is the
dangerous kind of divergence named in the brief: if a JWT-scope check is ever
added to the adapter, only this one file's fixture would happen to satisfy
it, and every other file's `_jwt` would start failing for a reason that looks
unrelated to what each test is actually about.

`_isolate_env(monkeypatch)` — identical in 4 files (`test_day_supervisor.py:350`,
`test_production_guard.py:22`, `test_live_app.py:17`, `test_tc_ent_10.py:100`),
each with a docstring literally cross-referencing another ("Same isolation as
test_live_app.py") — a self-documented, never-finished consolidation.

`_cert_env(monkeypatch, tmp_path)` — identical in 3 files
(`test_day_supervisor.py:366`, `test_live_app.py:32`, `test_tc_ent_10.py:173`).

`_drive(clock, coro)` — same polling-advance technique, different constants:
`test_entry_pipeline.py:25` (2000 × 5s = 10,000s simulated budget) vs.
`test_live_fill_path.py:45` (3000 × 1s = 3,000s budget).

Clock test-doubles: only `tests/adapters/test_broker_intent_contract.py`,
`tests/application/test_entry_pipeline.py`, and one other of the 3
importers actually use the shared `tests/harness/fake_clock.py::FakeClock`.
Everything else (`test_clocks.py:42 _FixedClock`, `test_live_runtime.py:24
FastClock`, `test_live_selection.py:175 _Clock`, `test_manual_adhoc.py:24
_Clock`, `test_manual_fire_shield.py:27 _Clock`, `test_tc_ent_08.py:51
_Clock`, `test_live_runtime_numbers.py:23 FastClock`,
`test_per_entry_wiring.py:138 _FastClock`, `test_dxlink_adapter_wiring.py:26
_RealClock`) reimplements its own. Several are legitimately simpler than
`FakeClock` (trivial `now()`-only stubs where no scheduling is exercised),
but `FastClock` (`test_live_runtime.py:24` / `test_live_runtime_numbers.py:23`)
is a verbatim duplicate — the second file's docstring says "see
test_live_runtime.py" instead of importing it.

**Consolidation**: add `FastClock` and a trivial no-op-`wait_until` clock to
`tests/harness/`; add `_jwt`/`_isolate_env`/`_cert_env`/`_drive` there or to a
new root `tests/conftest.py` (currently only `tests/bdd/conftest.py` exists —
there is no top-level conftest at all). Risk: SAFE-NOW, mechanical — but
**first** resolve the `test_tastytrade_adapter.py` scope-claim divergence
deliberately (keep it as an explicit parameter, e.g. `_jwt(iss, scope=None)`)
rather than silently dropping the one file that differs.

### 1.5 Close-input assembly — already properly consolidated (no finding)

Checked as requested: `composition/close_assembly.py::assemble_close_inputs`
is the single implementation, and it's shared correctly by
`composition/panel_commands.py:93` (manual close), `composition/paper.py:103`
and `composition/live.py:128` (STP-04 auto-flatten). No inline copies found.
This consolidation (called out in the code's own comments as deliberate,
post-incident) has held — good precedent for #1 and #2 above.

### 1.6 Frontend `api.ts` — no finding

`frontend/src/api.ts` has exactly one `get`/`post`/`getText` wrapper trio and
one `api` object; every endpoint call goes through it. No duplicate fetch
logic found elsewhere in `frontend/src` (`App.tsx`, `useLiveBot.ts`,
components all import from `api.ts`). Clean.

### 1.7 Time/ET handling — no finding

Backend: `ET = ZoneInfo("America/New_York")` defined exactly once
(`composition/live_gates.py:25`); `server.py` calls `datetime.now(ET)` in 4
places (`server.py:750,840,864,886,919`) — repetition, not duplication of
logic (trivial one-liner), low value to extract further.
Frontend: `frontend/src/time.ts` is the single ET/`Intl.DateTimeFormat`
consolidation point; no other file computes a timezone conversion.

### 1.8 Event-folding vs. `domain/projection.py` — mostly healthy

`reporting/folds.py` builds correctly on top of `domain/projection.py::fold`
(calls `fold(events)` at `folds.py:50,58`) rather than re-deriving entry
state — this is the right layering. One borderline case:
`adapters/api/server.py:273-274`'s `_remaining_rows()` manually filters
`CondorFilled`/`EntrySkipped` into raw sets rather than using `fold()`, but
this is deliberate (it needs distinct entry-id/skip-tuple semantics that
`fold()`'s `EntryProjection` doesn't expose directly) — not flagged as a
problem, just noted as reviewed.

---

## 2. Dead / vestigial code

### 2.1 `ChainSnapshot.put_band` / `call_band` — diagnostics with no consumer

`adapters/dxlink/chain_snapshot.py:44-45` (fields), `:77-78` (computed every
snapshot), `:181-188` (threaded through `snapshot_chain`). The module
docstring (`:14-20`) claims they're "kept for the live P/L card and
contract-test visibility." Grep confirms:
- **Zero** reads of `.put_band`/`.call_band` anywhere in
  `backend/src/meic` outside `chain_snapshot.py` itself — including
  `server.py`'s `_live_pnl_enricher`/`_sample_marks_once`, which is the code
  the comment claims consumes them.
- Only test files reference them:
  `tests/adapters/test_entry_mark_sampler.py`,
  `tests/adapters/test_live_pnl_enricher.py`,
  `tests/application/test_live_selection.py`,
  `tests/contract/test_live_selection_cert.py`.

STK-10 explicitly no longer gates on them (v1.51, `domain/chain.py`'s
trade-relative `reachable_strikes` superseded the fixed band). **Comment is
stale; fields are dead weight** — computed on every live chain snapshot for
no production consumer. SAFE-NOW to remove the two fields + their computation
in `build_sides()`, but touches 4 test files that assert on them directly →
labeled NEEDS-TESTS (trim/update those assertions in the same change).

### 2.2 Pre-v1.53 durable-id fallback (transitional, not yet dead)

5 call sites carry "row has no `id` key yet, pre-v1.53" compatibility logic:
`adapters/api/app.py:425`, `application/schedule_service.py:85` and
`:225-226`, `composition/live_runtime.py:83`,
`composition/live_wiring.py:148-157`. Each is currently load-bearing (any
schedule saved before v1.53 and never resaved since would break without it),
so **not dead yet** — but it's scattered, not centralized, and is exactly the
kind of shim that outlives its usefulness silently. See action #7.

### 2.3 `MutableClock` / demo-era code — legitimate, not dead

`application/clocks.py:34-42` (`MutableClock`) and `composition/runtime.py`
(`PaperDemoRuntime`) are actively wired into the still-supported `paper_app()`
entrypoint (`server.py:90-146`, `MutableClock` constructed at `server.py:100`,
`PaperDemoRuntime` at `server.py:102`, started at `server.py:137`). This is
the officially documented "Paper (SIM-01) — a self-driving demo day" mode
(`server.py:1-5`), not leftover scaffolding. Not flagged as dead; noted only
because it does bake hardcoded demo data (fixed date `2026-07-07`, 6 fixed
entry times, canned condor prices) directly into `server.py:100,107,66` —
low-value to change, mentioned for completeness only.

### 2.4 Frontend exported types — no dead exports found

Checked every `types.ts` export with 0 direct external references
(`BlockingState`, `PreflightCheck`, `StopOutSlippage`, `CorrectionEntry`,
`TaxonomyResult`, `CoreResults`, `DayEntryDetail`): all are reachable as
nested fields of larger exported interfaces (`PanelState.blocking_state`,
`Preflight.checks`, `ReportSummary.core`/`.taxonomy`,
`DaySlippageFamilies.stop_outs`, `DayReportDetail.entries`/`.corrections`),
and those parent types are each consumed by 1-6 component files. No orphaned
exports in the frontend.

### 2.5 Config keys — no orphans found

`config/validation.py` tombstones (`TOMBSTONE_KEYS` for RSK-02,
`TOMBSTONE_KEYS_V151` for `chain_atm_band_pts`) correctly **reject** stale
keys rather than silently ignore them — good pattern, not a smell. All 14
`MEIC_*` env vars referenced in `backend/src/meic` (grepped exhaustively)
are read by name in `server.py`'s config helpers; none found dangling.

---

## 3. Layering / health

### 3.1 `adapters/api/server.py` is oversized and mixes layers (933 LOC, largest file in repo)

Breakdown by responsibility (line ranges approximate):
- Env/config parsing: `_read_env`, `_chain_completeness_pct`,
  `_entry_window_seconds`, `_chain_retry_seconds`,
  `_reporting_capital_base`, `_sharpe_risk_free_pct`,
  `_report_min_sample_days`, `_reporting_config`, `_current_stop_loss_pct`
  (`server.py:38-236`, ~10 pure functions, no FastAPI dependency — could live
  standalone).
- Paper demo app factory: `paper_app()` (`server.py:90-146`).
- Day-supervisor orchestration: `_remaining_rows`, `_day_status_extras`,
  `_supervise_once`, `_supervisor_tick` (`server.py:257-365`) — **this is
  application-layer scheduling logic living in the adapter module**. Tell:
  `tests/application/test_day_supervisor.py:16-22` imports
  `_remaining_rows`/`_day_status_extras`/`_supervise_once`/`_supervisor_tick`
  directly `from meic.adapters.api.server` — the test is filed under
  `tests/application/` (acknowledging what layer this logic conceptually
  belongs to) while the code itself sits in `adapters/api/`. This is a
  concrete violation-in-spirit of doc 05's hexagonal boundary (adapters
  should translate HTTP <-> application services, not contain new
  application logic no `application/` module owns).
- Live P/L plumbing: `_BrokerReadFacade`, `_maybe_eod_reconcile_once`,
  `_leg_mid`, `_live_pnl_enricher`, `_sample_marks_once`
  (`server.py:367-521`).
- Live day wiring: `_wire_live_day`, `_max_entries` (`server.py:522-637`) —
  this one is legitimately thin per its own docstring ("every decision that
  could leave a safety rail unarmed lives in composition/live_wiring.py");
  it correctly delegates to `composition/live_wiring.py`'s
  `build_live_runtime`/`build_manual_entry`/`live_preflight_checks`. Good
  precedent for the same treatment of the day-supervisor block above.
- `live_app()` factory + routes (`server.py:639-933`).

**Recommendation**: extract the day-supervisor block into a new
`application/day_supervisor.py` (mirroring how `live_wiring.py` already
holds the safety-rail assembly logic), and the env/config parsing block into
`adapters/api/server_config.py` (pure functions, easy lift). Leaves
`server.py` as: two app factories + route registration, which is what an
"adapters/api" module should be. Risk: NEEDS-TESTS — `test_day_supervisor.py`
and `test_live_app.py` import these names from `server`; moving them means
updating those imports (mechanical) and re-running the two files as the
acceptance gate. No behavior change intended.

### 3.2 Decimal / float hygiene — no leaks found

Grepped for `float(` conversions of money-shaped values across
`backend/src/meic`; the only `float()` uses found are for clock-drift
milliseconds (`live_wiring.py:84-91`, legitimately time math, not money) and
env var parsing of non-money tunables (`_entry_window_seconds`,
`_chain_retry_seconds`, `max_drift_ms`). All P&L/credit/price arithmetic
observed in `domain/projection.py`, `reporting/folds.py`,
`application/execute_entry.py` consistently uses `Decimal`. No float leak
found in the sampled modules.

### 3.3 asyncio hygiene — reviewed, no problems found

- Broad `except Exception:` sites (4 total):
  `adapters/persistence/event_store.py:50` (rolls back then **re-raises** —
  correct), `adapters/tastytrade/adapter.py:48` (best-effort JWT-issuer
  parse, returns `None` — correct for a diagnostic helper),
  `adapters/tastytrade/adapter.py:242` (falls through to a documented
  `_replace_fallback` — correct, commented), `application/protect_position.py:167`
  (retry loop, submit failure treated as "no order id yet, retry" — correct,
  commented with the STP semantics it protects). All 4 are deliberate and
  commented; none silently swallow without a documented reason.
- `asyncio.create_task` call sites (`server.py:137,348,783,869,921`) all
  retain the task reference on `app.state`/`app_state` — no fire-and-forget
  GC risk.
- `asyncio.ensure_future` in `application/manual_entry.py:235` and
  `composition/live_runtime.py:219` are both explicitly commented as
  intentionally-shielded single units (post-2026-07-09-incident-review
  design: "cancelling mid-ladder would orphan a live resting order at the
  broker") with `add_done_callback` alerting on orphaned failure. This is
  good, hard-won design — not a hygiene problem.

### 3.4 TODO / OPEN ITEM inventory

Only 3 explicit `OPEN ITEM` comments in the entire backend (no bare `TODO`/
`FIXME`/`XXX` found at all):
- `application/protect_position.py:200` — doc 06's `unprotected_action`
  side-scoped flatten, flagged not resolved.
- `composition/paper.py:82-101` and `composition/live.py:107-126` — the
  identical OPEN ITEM described in action #9 above (same issue, same text,
  duplicated by virtue of finding #1.1).

Small inventory, cleanly flagged — good practice already in place; just
duplicated across the two composition roots.

---

## 4. Test health

- **Fixture duplication**: see §1.4 (`_jwt` × 7, `_isolate_env` × 4,
  `_cert_env` × 3, `_drive` × 2, clock doubles × ~9). No root-level
  `tests/conftest.py` exists at all — only `tests/bdd/conftest.py`. Adding
  one would be the natural home for the cross-cutting helpers.
- **Private-attribute assertions**: `tests/adapters/test_entry_mark_sampler.py`,
  `test_live_pnl_enricher.py`, `test_live_selection.py`,
  `test_live_selection_cert.py` assert directly on `ChainSnapshot.put_band`/
  `.call_band` — diagnostics with no production consumer (§2.1). These
  assertions are testing dead product surface; trimming them is part of the
  §2.1 cleanup, not a separate test-seam problem.
- **Contract-deselected tests (13, 4 files)**: currently healthy (last
  touched within the last 2 days per `git log`), but structurally at risk of
  rotting silently since they only run when explicitly invoked with
  `pytest -m contract` against live sandbox credentials — nothing in CI
  proves they still pass against the current adapter code between operator
  runs. No action needed today; worth a periodic-run reminder if the gap
  between operator-triggered runs grows.
- **Slowest test**: `tests/bdd/test_tc_ui_05.py` at 4.37s / 29% of total
  suite wall time — by design (shells to real vitest), not a target for
  optimization; see baseline note above.
- **Test-to-code ratio**: 19,112 test LOC vs. 11,747 backend src LOC ≈
  **1.63:1**. Frontend: 1,317 test LOC vs. ~3,629 non-test src LOC ≈ 0.36:1
  (component tests are comparatively thinner than the backend's BDD-heavy
  suite — consistent with a spec-driven backend and a thinner, more
  conventional frontend).

---

## 5. Metrics

### Backend LOC by layer (`backend/src/meic`)

| Layer | Files | LOC |
|---|---|---|
| domain | 24 | 2,117 |
| application | 41 | 4,130 |
| adapters | 17 | 3,140 |
| composition | 10 | 1,424 |
| reporting | 12 | 852 |
| **total** | **108** (incl. `__init__.py`s) | **11,747** |

### Largest backend files

| File | LOC |
|---|---|
| `adapters/api/server.py` | 933 |
| `adapters/api/app.py` | 536 |
| `adapters/tastytrade/adapter.py` | 417 |
| `adapters/api/reports.py` | 370 |
| `domain/events.py` | 355 |
| `application/execute_entry.py` | 338 |
| `application/schedule_service.py` | 328 |
| `composition/live_wiring.py` | 293 |
| `adapters/persistence/event_store.py` | 275 |
| `application/manual_entry.py` | 256 |
| `adapters/sim/simulated_broker.py` | 253 |

### Largest frontend files

| File | LOC |
|---|---|
| `components/SchedulePanel.tsx` | 419 |
| `types.ts` | 371 |
| `components/SchedulePanel.test.tsx` | 331 |
| `components/results/ResultsPage.tsx` | 277 |
| `App.tsx` | 268 |
| `components/ManualTradeCard.tsx` | 266 |

`SchedulePanel.tsx` (419 LOC) is the largest frontend component and has the
largest test file to match (331 LOC) — a candidate for future decomposition
if it grows further, but not flagged as unhealthy today (no duplication
found inside it during this pass; a deeper component-level review was out of
scope given the time budget).

### Test counts

- Backend: 146 test files, 19,112 LOC, 976 passing + 13 contract-deselected.
- Frontend: 13 test files, 1,317 LOC, ~98-123 individual test cases
  (`it`/`test` call sites).

---

## Summary of consolidation risk levels

| # | Finding | Label |
|---|---|---|
| 1 | paper.py/live.py shared logic extraction | SAFE-NOW |
| 2 | `_order_id`/broker-shape normalizer merge | SAFE-NOW (NEEDS-TESTS for the `_fill_matches`/`_symbol_and_signed_qty` generalization) |
| 3 | server.py split (day-supervisor + config) | SAFE-NOW extraction / NEEDS-TESTS for import updates |
| 4 | `put_band`/`call_band` removal | SAFE-NOW code / NEEDS-TESTS (4 test files) |
| 5 | Test helper consolidation (`_jwt` et al.) | SAFE-NOW (resolve the scope-claim divergence deliberately first) |
| 6 | Clock test-double consolidation | NEEDS-TESTS (mechanical but touches ~9 files) |
| 7 | Pre-v1.53 durable-id shim | SAFE-NOW to centralize / NEEDS-RATIFICATION to retire |
| 8 | `_drive()` harmonization | SAFE-NOW |
| 9 | `flatten_side` unprotected-close gap | NEEDS-RATIFICATION (spec-adjacent behavior change) |
| 10 | Dead docstring-as-statement in paper.py | SAFE-NOW (one-line deletion, or fixed for free by #1) |

This report recommends only; no files outside this report were modified.
