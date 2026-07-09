# Spec amendment proposal — TC-ORD-08 / TC-STP-19: fill credit is the broker's

**To:** Ash (operator / spec owner) — for the adviser bot to draft the Gherkin
**From:** coding agent
**Re:** two new test cases for `spec/04-test-cases.md` covering the 2026-07-09
fill-credit incident
**Status:** PROPOSED — awaiting Gherkin + ratification. The fixes are already
implemented and unit-tested (commit `dbc8115`); this adds the spec-level lock so
the behaviour can never silently regress.

---

## The incident being encoded (2026-07-09, live production fill)

Order 482390058: the reprice ladder's final rung was a **3.50 credit** limit.
The broker's actual per-leg fills: sold 1.80 + 1.95 = 3.75, bought 0.08 + 0.07 =
0.15 → **actual net credit 3.60** (price improvement). Two defects followed:

1. The bot recorded **3.50** (the limit it *asked* for) as the entry's net
   credit — the dashboard disagreed with the broker by $0.10.
2. Worse: the protective stops were computed from the **pre-fill mid estimate**,
   not the actual credit received — STP-02 says *"trigger = pct × net credit"*,
   so the 95% stop rested at the wrong level.

## Proposed test cases (suggested IDs are the next free in each series)

### TC-ORD-08 — recorded fill credit is the broker's, never the order's

Scenario sketch for the Gherkin (adviser to phrase in house style):

- **Given** a 4-leg entry limit working at a 3.50 credit
- **And** the broker reports per-leg fill allocations: shorts 1.80 and 1.95,
  longs 0.08 and 0.07
- **When** the fill is recorded
- **Then** the entry's net credit is **3.60** — the sum of broker-allocated leg
  prices (shorts − longs), never the working limit price

Second scenario (the honesty fallback, STP-02d):

- **Given** a fill where the broker reports **no allocation** for at least one
  leg (the paper/simulated case, or a live payload gap)
- **When** the fill is recorded
- **Then** the working limit price is recorded as the credit
- **And** no per-leg price is fabricated (a fabricated allocation would poison
  the exact field STP-02d exists to reconcile)

### TC-STP-19 — stop triggers are computed from the ACTUAL credit received

- **Given** an entry that filled at an actual net credit of 3.60
- **And** stop basis `total_credit` with `stop_loss_pct` 95
- **When** the protective stops are placed
- **Then** the trigger derives from 95% × **3.60** (the actual fill credit),
  never from the pre-fill mid estimate

Rationale line for both: live incident 2026-07-09 (order 482390058 — limit
3.50, broker-allocated net 3.60; stop had rested at pct × mid).

## Where each layer lives (the locked pipeline)

1. Adviser's Gherkin → **`spec/04-test-cases.md`** (ratified; reviewer
   regenerates `spec.lock.json`).
2. `python scripts/extract_features.py` → **`tests/features/TC-ORD-08.feature`**
   / **`TC-STP-19.feature`** (generated, locked).
3. Coding agent then writes the step definitions in **`tests/bdd/`** binding the
   scenarios to the real pipeline; `scripts/check_traceability.py` enforces
   their existence from then on.

## Already in place behind this proposal (commit `dbc8115`)

- `execute_entry._record_fill`: net credit computed from broker-allocated leg
  prices when every leg carries one; rung-price fallback otherwise.
- `live/paper _on_filled(fill_credit=...)`: actual credit threaded into
  `protect()` as `total_net_credit` (STP-02).
- Unit tests already pin both behaviours
  (`tests/application/test_entry_pipeline.py`,
  `tests/application/test_compositions.py`).

Known adjacent gap, deliberately out of scope here: the *per-leg* fill values
handed to the stop calculator still use mids (only affects the non-default
`short_premium` basis) — flag for a future ruling if `short_premium` is ever
used live.
