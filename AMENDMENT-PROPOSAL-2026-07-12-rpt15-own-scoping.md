# Spec amendment proposal — RPT-15 own-order scoping & PNL-04 broker truth

**To:** Ash (operator / spec owner) → reviewer bot, for hardening into `spec/`
**From:** coding agent
**Date:** 2026-07-12
**Status:** BUILT and deployed on `live-testing-2026-07-09` (commit `56d99e1`); verified
against the live production account. `spec/` is hash-locked and was NOT touched.
This document asks the reviewer to ratify the rule text and the test cases so the
behaviour is locked in `spec/` rather than resting only on agent-written tests.

---

## 0. The incident (what the operator actually saw)

On 2026-07-12 the operator noticed the Results dashboard showed **2026-07-10** as
`net $40.00 / gross $40.00 / fees $0.00` — net equal to gross, with zero fees on a
real live trade. Investigation found RPT-15 had *also* journaled a `CorrectionRecord`
asserting the broker's `cash_delta` for that day was **−534.46**.

**Both numbers were wrong.** The bot's condor actually made **+$43.68** (gross
$50.00, fees $6.32), confirmed leg-by-leg against tastytrade's transaction history.

The operator caught the agent's own misdiagnosis: the agent initially trusted the
−534.46 as broker truth and reverse-engineered a fictional market scenario to
explain it. The operator supplied the decisive fact (SPX closed **7575.26**; the put
spread expired worthless), which broke the analysis open. **Recorded here because it
is the strongest argument for the rules below: a wrong "broker truth" figure is more
dangerous than no figure at all — it is actively believed.**

---

## 1. Root cause

The bot **shares its brokerage account with the operator's own trading** — already
first-class in the spec (**v1.49**, "single-account operation is first-class"; **OWN-01**,
**OWN-03**, and the **EOD-03 sweep** all exist precisely because of this).

RPT-15's reconciler fetched `day_fills` / `cash_and_fees` for the **ENTIRE ACCOUNT**
and treated the sum as "the broker's version of the bot's day". On 2026-07-10 the
account also held **the operator's own trades**:

```
  +43.68   the bot's condor            (orders 482621396 / 482621556 / 482760202)
 +350.12   the operator's 7580 condor  (order 482759560)   <- NOT the bot's
 -466.00     └─ its P7580 short settled ITM                <- NOT the bot's
 -928.26   the operator's MNQ futures put (order 482542569) <- NOT the bot's
 ────────
 -534.46   <- what RPT-15 called "broker truth"
```

This **violates OWN-01**: *"the ledger is built exclusively from fills on its own
order IDs; operator/manual trades never enter the ledger, its P&L, or its risk
marks."* RPT-15 was never scoped, and **OWN-03** was never applied to it.

**The enabling defect:** the bot **never journaled its own entry order ID**. The code
said so explicitly (`server.py`): `own |= registry.order_ids()  # a live entry ladder's
id is journaled nowhere`. `CondorFilled` had no order-id field at all. Stops (v1.60),
decay buybacks (v1.61) and LEX orders (v1.62) journal theirs — **the entry never did.**
So the reconciler *could not* have scoped even if it wanted to.

---

## 2. Defects found, and the rules proposed to lock each

### D1 — The entry order ID is never journaled
**Proposed rule — ORD-10 (Own-order identity):** *Every order the bot submits MUST
journal its broker order id, INCLUDING the entry (condor) order. `CondorFilled`
carries `broker_order_id`. On a shared account this is the ONLY thing that lets the
bot distinguish its own fills from the operator's (OWN-01/OWN-03); an order the bot
cannot name, it cannot account for.*
**Built:** `CondorFilled.broker_order_id` (additive/optional), populated from the
`working_id` already in scope at `_record_fill`.

### D2 — RPT-15 reconciled the whole account
**Proposed rule — RPT-15a (Own-order scoping):** *RPT-15's `cash_delta`, `fees` and
`fill_count` MUST be computed ONLY from broker transactions whose order id is one the
bot itself journaled placing. Settlement rows (which carry no order id) are attributed
by symbol against the bot's own fills. A symbol claimed by BOTH an own fill and a
foreign fill on the same day is genuinely ambiguous: it is EXCLUDED and COUNTED
(`ambiguous_settlements`), never guessed — the same OWN-03 shared-symbol guard RPT-16
already uses. `positions()` (the whole-account `flat` check) is unchanged.*

### D3 — Fees summed from an enumerated component list
**Proposed rule — PNL-01a (Fee derivation):** *A broker transaction's fee is derived as
`value − net_value` (the broker's own invariant `net_value = value + fees`), never
summed from a list of named fee categories. A component sum silently goes light the day
the broker adds a category — and RPT-15 would then "confirm" a wrong number.*
Verified on the real rows: fill (10.00 − 9.28 = 0.72), settlement (−461.00 − (−466.00)
= 5.00); the day's six own rows sum to exactly **6.32**.

### D4 — Comparison was string-based, not numeric
**Proposed rule — RPT-15b (Comparison semantics):** *Reconcile comparisons are NUMERIC
(Decimal). The bot folds at scale 4 ("6.3200"); the broker reports scale 2 ("6.32").
A string compare calls these different and emits a `CorrectionRecord` whose own `diff`
is `0.0000` — a correction that corrects nothing, on a day that reconciled perfectly.
No epsilon and no rounding: a genuine one-cent disagreement still corrects.*
**Evidence in the live journal:** a 2026-07-11 record reads `fees: bot "0" vs broker
"0.0", diff "0.0"`.

### D5 — `fill_count` compared entries against leg rows
**Proposed rule (folded into RPT-15b):** *`fill_count` compares LIKE FOR LIKE: the bot's
filled-ENTRY count against the number of distinct broker ENTRY orders that produced
fills. The broker returns one row per LEG (the real 2026-07-10 day is 1 entry but 6 own
rows), so counting rows disagrees on every real day — and because corrections are
RENDERED, the dashboard would have displayed "6 fills" for a one-condor day.*

### D6 — A day it cannot attribute, it invented a number for
**Proposed rule — RPT-15c (Unattributable day):** *If the bot filled entries on a day but
NONE of that day's entries carries a journaled broker order id, the day is
UNATTRIBUTABLE. RPT-15 MUST journal NOTHING (no `CorrectionRecord`, no
`DayBrokerConfirmed`), leave the day unstamped, and raise ONE critical alert. It MUST
NOT report the broker's side as zero. "The broker showed $0" and "I cannot tell which
rows are mine" are different claims, and only one of them is true.*
Without this guard the reconciler would have journaled, as broker truth:
`cash_delta: bot='43.6800' broker='0'` — **on a day the broker in fact reported +43.68.**

### D7 — Broker truth never reached the dashboard (PNL-04 unimplemented)
**PNL-04 already says** *"the broker-derived figure is what the day report and all
stored history present as authoritative."* The override helper
(`reporting/corrections.corrected_value`) existed — and was **never called by the
report**. The dashboard always showed the bot's own projection.
**Proposed clarification — PNL-04a:** *`core_results` / `daily_net` apply the own-scoped
broker figures per day. Absent an own-scoped correction, the plain fold renders
(byte-identical), badged bot-computed (UI-25).*

### D8 — Legacy corrections are poison and must be permanently inert
The append-only journal **still contains** the pre-fix polluted records (2026-07-10:
`cash_delta = −534.46`). Applying corrections naively would have displayed **−$534 on a
+$43.68 trade**.
**Proposed rule — RPT-15d (Correction trust):** *A `CorrectionRecord` carries its
`scope`. Only `scope="own"` (written by the own-scoped reconciler) may be DISPLAYED. A
record without it is a legacy artifact of the whole-account bug: it remains in the log
(history is never rewritten) but is permanently inert. The EOD gate likewise does not
count a legacy record as a completed reconciliation — such a day stays eligible.*

### D9 — No on-demand reconcile existed
PNL-04 says the pass runs *"At EOD (and on demand)"*. There was no on-demand trigger.
**Built:** `POST /reports/reconcile/{day}`, auth-gated as every other operator command.

### D10 — Pre-journaling days can never self-attribute
**Proposed rule — RPT-16a (Order-id backfill):** *For a day predating order-id
journaling, the operator MAY supply the bot's broker order ids (the same
operator-supplied-ids escape hatch RPT-16 already ratifies for imports). The backfill
event is METADATA-ONLY: it carries no money and is folded into NO P&L projection
(the entry's own events remain the sole money record), and it is idempotent.*
**Built:** `OwnOrderIdBackfilled` + `backfill_own_order_ids`. Test-pinned that every
money fold is **byte-identical** before and after — and verified so on the live journal.

---

## 3. Proposed test cases (for the reviewer to draft into `spec/04-test-cases.md`)

`tests/features/` is generated from the spec, so these must come from the reviewer. All
already have agent-written equivalents (named below) that can be lifted.

- **TC-RPT-17 — shared-account scoping (RPT-15a/OWN-01/OWN-03).** A day where the account
  holds the bot's condor AND an operator-only trade. RPT-15's `cash_delta`/`fees` reflect
  ONLY the bot's rows; the foreign trade never moves them; a clean day emits no correction.
  *(Real vector: bot +43.68 / fees 6.32; foreign MNQ −928.26 and foreign settlement −466.00
  excluded.)* → `test_shared_account_reconcile_ignores_the_operators_foreign_rows`
- **TC-RPT-18 — unattributable day is refused, never fabricated (RPT-15c).** Entries exist,
  no order ids journaled ⇒ status `unattributable`, ZERO events appended, ONE alert.
  → `test_a_day_whose_entries_journaled_no_order_id_is_refused_never_fabricated`
- **TC-RPT-19 — ambiguous settlement is never guessed (OWN-03).** A symbol on both an own
  and a foreign fill ⇒ its settlement excluded and counted.
  → `test_a_symbol_shared_between_an_own_fill_and_a_foreign_fill_is_never_guessed`
- **TC-RPT-20 — legacy corrections can never be displayed (RPT-15d).** A `scope`-less
  `CorrectionRecord` (broker −534.46) NEVER overrides the fold; an own-scoped one does.
  → `test_core_results_never_lets_a_legacy_unscoped_correction_override`
- **TC-RPT-21 — PNL-04 broker truth on the dashboard (PNL-04a).** With own-scoped
  corrections, the day report shows net 43.68 / fees 6.32 / gross 50.00; with none, the
  fold renders byte-identical.
  → `test_core_results_applies_an_own_scoped_correction_to_the_real_2026_07_10_vector`
- **TC-RPT-22 — order-id backfill is money-neutral (RPT-16a).** Every money fold is
  byte-identical before/after the backfill; idempotent.
  → `test_backfill_changes_no_money_fold_byte_for_byte`
- **TC-PNL-02 — fee derivation (PNL-01a).** Fee = `value − net_value`, exact to the cent on
  the real rows (0.72 / 0.72 / 5.00 → day total 6.32).

---

## 4. Verification against the live account

| Day | Before | After | Broker truth |
|---|---|---|---|
| 2026-07-10 | net 40.00 / gross 40.00 / fees 0 | **net 43.68 / gross 50.00 / fees 6.32** | ✅ exact match |
| 2026-07-09 | net −13.88 | unchanged | ✅ already correct (imported, RPT-16) |

Backend **1284 passed**; spec lock **17/17**; traceability **220/152**.

---

## 5. 2026-07-09 — RETRACTED claim, and the real hardening item it exposed

### 5a. Retraction (agent error, operator-corrected)
An earlier draft of this document claimed *"no stop ever fired ⇒ the short was
unprotected."* **That claim is WRONG and is withdrawn.** It was inferred from the absence
of a Buy-to-Close row in the broker's transaction history. **A resting stop that is never
triggered produces no transaction** — so its absence proves the stop *was not hit*, not
that it did not exist. The operator confirmed a stop was resting and its trigger was
never reached. **The system behaved correctly on 2026-07-09.** No protection defect.

### 5b. What actually happened (for the record)
The condor's body was 5 points wide (PUT short 7535 / CALL short 7540; wings 7510/7565),
credit **3.60**. SPX settled **7543.64** — **3.64 points through the CALL short**. The
long 7565 wing gives no protection at 7543 (it only bites above 7565), so the short's
intrinsic was paid in full: **−$364**.

Upper gross breakeven = `7540 + 3.60` = **7543.60**. SPX settled **7543.64** — the trade
expired **0.04 of an index point** past breakeven. Net breakeven (incl. fees) ≈ 7543.50.

```
 +360.00 credit  −364.00 ITM call short  −9.88 fees  =  −13.88
```
Not a bug, not a strategy failure: a trade that finished a fraction of a point the wrong
side of its breakeven. Every mechanism — stop, fills, settlement — worked as designed.

### 5c. THE REAL HARDENING ITEM (proposed: PNL-05)
This day *is* the dangerous reporting shape, for a different reason. **When a stop is
never hit, the short is held to expiry and its loss arrives ONLY as a settlement.** Until
that broker settlement row is captured, the bot's own fold sees only the credit — i.e. it
would report **+$360 profit on a day that actually lost $13.88.**

EOD-01 v1.59 already says such a P&L is *"PROVISIONAL and labeled so"*, and the per-entry
card and day drill-down DO render `provisional — settlement pending`. **But the Results
headline band (NET P&L / GROSS / FEES) has no settlement-pending awareness at all** — it
will present a credit-only figure as if final.

**Proposed rule — PNL-05 (Provisional day):** *A period whose scope contains ANY entry with
an uncaptured settlement (`settlement_pending`) MUST label its headline P&L PROVISIONAL.
A held-to-expiry ITM short's loss arrives only with its settlement; until then the
headline is a credit-only figure and MUST NOT be presented as final. This is the
"stop never hit" shape and it must never masquerade as a profit.*

**Proposed TC-PNL-03:** a day with one entry, stop not hit, short held to expiry, settlement
NOT yet captured ⇒ the summary is flagged provisional; once the settlement is captured the
flag clears and the day reports the true (loss) figure. *(The settled half is already pinned:
`test_pinned_2026_07_09_vector_nets_minus_13_88_once_settled`.)*

Backstop already in place: the now-own-scoped RPT-15 reconcile compares the bot's fold
against broker truth at EOD and corrects it (PNL-04) — so the error is caught. PNL-05
closes the window BEFORE that reconcile runs.
