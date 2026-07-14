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

## 4b. STP-02b buffer — CORRECT but UNPINNED (2026-07-13, operator-requested)

**Operator concern (2026-07-13, before a live trade with `stop_rebate_markup = 0.30`):**
*"I want to make sure when the buffer is $0.30 then that's what happens, not $0."*

**Finding: the behaviour is CORRECT. The GUARANTEE was missing.**

Traced and executed against production code:
```
live_runtime.stop -> StopParams(markup = entry.stop_rebate_markup)   # the ROW's value
  execute.attempt(..., stop=row.stop)  ->  comp._on_filled(..., row.stop, ...)
live._on_filled   -> protect(markup = stop.markup)
stop_policy       -> trigger = floor_to_tick(pct% x net_credit + markup)
```
Real function, real tick table: credit 5.20 / 95% / markup 0.00 -> **4.90** (byte-matches the
2026-07-10 journal's recorded trigger); markup 0.30 -> **5.20**. The buffer is applied.

**But NOTHING pinned it end-to-end.** `TC-STP-14` exercises only the PURE FUNCTION
(`stop_trigger(markup=0.50)`); `test_protect_position` passes `markup=` in BY HAND. No test
proved a **schedule row's** `stop_rebate_markup` survives `row -> fire -> fill -> protect ->
placed stop`. It could have silently regressed to **$0.00** at the broker with a fully green
suite — a real-money stop resting at the wrong level, invisibly.

**Built (tests only — ZERO production change; `git diff` on production files is empty):**
`tests/application/test_stop_buffer_reaches_the_broker.py` drives a real `LiveComposition`
(only the network-facing broker faked) end-to-end:
1. the operator's exact live vector (credit 5.20, 95%, buffer 0.30) lands the raised trigger at
   the broker, on BOTH shorts, with `StopPlaced.markup == 0.30`;
2. zero-buffer control (same row) -> 4.90;
3. the delta is exactly the buffer at this credit — with the invariant stated correctly:
   *"markup is applied BEFORE flooring"*, never *"delta always equals markup"* (flooring can
   absorb part of it at other credits — cf. TC-STP-14 scenario 1);
4. the MANUAL/ad-hoc fire path (a SEPARATE call site, `manual_entry._stop`) pinned independently.

**Fail-first proof:** forcing `markup=Decimal("0")` in `composition/live.py::_on_filled` makes the
0.30 tests FAIL (the zero-buffer control keeps passing, as it must) — proving they would catch a
silent regression to $0. Production line restored byte-for-byte.

**Proposed TC-STP-14a (for the reviewer):** *a schedule row's `stop_rebate_markup` MUST reach the
placed broker stop — pinned end-to-end through the scheduled AND manual fire paths, not merely at
the pure-function level.* Rule text unchanged (STP-02b is correct); this closes a COVERAGE hole,
which is the class of hole that lets correct code silently rot.

**Calibration note for the operator (not a defect, an observation):** the buffer pre-credits the
expected long recovery. The only real recovery observed (2026-07-10, per the broker) was **$0.10**
(the orphaned C7595 long sold at 0.10), against a buffer set to **$0.30** — a ~$0.20/side shortfall
if it repeats. RPT-07's long-recovery report shows **zero rows** for that day because the bot never
journaled the `LongSold`, so the system cannot yet calibrate this from its own data. Worth the
reviewer's attention: the buffer is only sound if the recovery it pre-credits actually arrives.

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

---

## 6. 2026-07-13/14 findings — an operator's out-of-band close, three unbuilt
## spec clauses found empty in the live journal, and a fifth+sixth "built,
## never wired" component

### 6.0 Status of the 2026-07-12 proposals (for the adviser: the code has moved: on)

Everything §2–§5 of this document proposed is now **BUILT** on `live-testing-2026-07-09`,
none of it yet **ratified** into `spec/` (which remains untouched, as required). For the
adviser's tracking:

| §  | Proposal | Status | Commit |
|---|---|---|---|
| D1 | ORD-10 (own-order identity, `CondorFilled.broker_order_id`) | BUILT | `56d99e1` |
| D2 | RPT-15a (own-order scoping) | BUILT | `56d99e1` |
| D3 | PNL-01a (RPT-15's fee derivation, `value − net_value`) | BUILT | `56d99e1` |
| D4/D5 | RPT-15b (numeric comparison, like-for-like fill_count) | BUILT | `56d99e1` |
| D6 | RPT-15c (unattributable day) | BUILT | `56d99e1` |
| D7 | PNL-04a (dashboard renders own-scoped correction) | BUILT | `56d99e1` |
| D8 | RPT-15d (correction trust / `scope`) | BUILT | `56d99e1` |
| D10 | RPT-16a (order-id backfill) | BUILT | `56d99e1` |
| §4b | TC-STP-14a (buffer pinned end-to-end) | BUILT, tests-only | `235dc56` |
| §5c | PNL-05 (provisional-day headline) | BUILT | `511a81e` |

**Important correction to carry forward from §4b.** That section's calibration note said
RPT-07 shows zero long-recovery rows for 2026-07-10 "because the bot never journaled the
`LongSold`" and treated this as an unrelated reporting gap. **Item A below shows this was
wrong in a specific way:** the bot never journaled a `LongSold` for that day because **there
was no bot recovery to journal — a human recovered it.** RPT-07's zero rows are therefore
*correct*, not a gap. The $0.10-vs-$0.30 buffer-shortfall concern in §4b's calibration note
is now MOOT for the 07-10 instance specifically (there is no bot recovery price to compare
the buffer against) but the general concern — *"the buffer is only sound if the recovery it
pre-credits actually arrives"* — stands unchanged for the next stop-out the bot itself
recovers.

None of D1–D10, PNL-05, or TC-STP-14a has been copied into `spec/` by the adviser as of this
writing — `01-strategy-rules.md`, `10-results-dashboard.md`, `06-configuration.md` and
`04-test-cases.md` still describe pre-fix behaviour throughout. RPT-15a/b/c/d, ORD-10, PNL-01a,
PNL-04a and RPT-16a's *mechanism* worked exactly as designed once built (D1–D10) — the new
defect in item A below is a **misuse of RPT-16a** (a human misattributed an order id it was
given), not a flaw in RPT-16a itself.

---

### A. OWN-01 — an operator's out-of-band close of a BOT leg (NEW, operator-ruled, needs ratifying)

**Spec today (`spec/01-strategy-rules.md:230`):**
> **OWN-01 Ownership ledger.** The bot maintains a per-symbol owned-quantity ledger built
> **exclusively from fills on its own order IDs** (event log). Operator/manual trades never
> enter the ledger, its P&L, or its risk marks (buying-power checks use broker reality;
> everything else uses the ledger).

**Evidence.** On 2026-07-10 the bot's CALL short stopped. Live stop-fill detection did not
exist yet (it landed later that same day, `6e70603`), so the bot never noticed its own stop
had filled and never ran the LEX ladder. **The operator watched, correctly concluded the bot
was not going to sell, and sold the long call himself from his own tastytrade platform**:
order `482760202`, `SPXW 260710C07595000`. A later RPT-16 order-id backfill (built per §D10,
operator-authorised) journaled that order as
`OwnOrderIdBackfilled(entry_id="2026-07-10#1", broker_order_id="482760202", role="lex")` —
**an assumption, not a fact**: the agent doing the backfill inferred "this must be the bot's
LEX order" from the fact that it closed a bot leg, and was wrong. The RPT-15 reconciler then
counted the operator's fill and fee as the bot's own — **the identical shared-account leak
class OWN-01/OWN-03 exist to prevent, reintroduced by hand inside the fix for it.** It looked
benign only because the operator's order happened to close a bot leg; had he traded anything
else with that order id nearby, the ledger would have been polluted exactly as the original
-$534.46 incident (§1) was.

**Operator ruling (2026-07-13, explicit): STRICT OWN-01.** The bot's ledger contains ONLY the
bot's own orders. The operator's fill is the operator's, even when it closes a bot leg.

**Built (commit `1436da4`):** an append-only `OwnOrderIdRetracted(entry_id, broker_order_id,
reason, at, note)` event — the mistaken `OwnOrderIdBackfilled` is never deleted or rewritten
(the log is append-only; drill-down history is preserved), this event withdraws its effect.
`reporting/own_orders.py::own_order_ids()` becomes `claimed − retracted`. **The trap, pinned
by a test:** the retraction event itself carries a `broker_order_id` field — the same generic
field `own_order_ids` scans for on every event — so a naive implementation adds back the very
id it exists to withdraw; the built version excludes `OwnOrderIdRetracted` from the "claimed"
side of the scan before subtracting. Also pinned: `corrected_value` renders the newest
`scope="own"` `CorrectionRecord` for a (day, field), so a fresh reconcile after retraction
supersedes the polluted one — both records remain in the append-only log.

**Deliberately does NOT journal a `LongSold`.** The bot did not recover that long — a human
did. Writing `LongSold` would mark LEX-07 satisfied, silence the new LEX-07 watchdog (item E)
that correctly would have caught this exact case, and erase the only record that the operator
had to rescue the trade. The commit's own words: *"A ledger that is $10 light and honest beats
one that is complete and fictional — that is exactly how -$534.46 came to be believed."*

**Falsifiable prediction, unverified as of this writing:** with the operator's order stripped,
2026-07-10 becomes a 5-contract shape (4 opens + 1 stop close) that the PNL-01 fee model
(item B) was never fitted to:
```
2 short opens 2×1.72 + 2 long opens 2×0.72 + 1 close 0.72 = 5.60   (not 6.32)
gross 40.00, net 34.40
```
This also **retracts an over-claim in commit `0d1825e`** (item B, and the original §D3 of this
document by extension): 2026-07-10 was NOT an independent second verification of the fee
model — it only looked like a 4-open/2-close shape because the operator's order supplied the
6th contract. 2026-07-13 remains a genuine, independent verification.

**Proposed rule — OWN-01a (Own-order retraction, ratifying the strict form):** *An id
previously journaled as the bot's own (directly, or via RPT-16a backfill) that is later
discovered to be an operator's out-of-band order MUST be withdrawn by an append-only
`OwnOrderIdRetracted` event, never by editing or deleting the original record. Retraction is
metadata-only — it is folded into no P&L projection and does not itself journal a `LongSold`
or any other recovery event for the affected side; the bot's ledger going light by the
retracted order's proceeds is the accepted cost of OWN-01's own-orders-only guarantee.*

**The open economic question, for the adviser to rule on — present BOTH forms:**

1. **Strict OWN-01 (built, operator-ruled 2026-07-13).** As above: the bot's ledger is light
   by the proceeds of any leg the operator closes out-of-band, even though the ACCOUNT
   received that cash. RPT-15's own-scoped `cash_delta`/`fees` will under-report the entry's
   true realized outcome by exactly the retracted fill's `net_value`. This is by design —
   OWN-01's own-orders-only boundary is absolute — but it means the dashboard's per-entry P&L
   for 2026-07-10 will read $34.40, not the $43.68 the account actually kept.
2. **Alternative — a distinct "operator out-of-band disposal" event (NOT built).** A new
   event type, e.g. `OperatorDisposalRecorded(entry_id, side, order_id, proceeds, fee, at)`,
   attributed to the bot's entry for NARRATIVE/audit completeness (so the entry's card can
   still show "long recovered — by the operator, not the bot, $9.28 net") but **structurally
   excluded from `own_order_ids()`/RPT-15 money-scoping and never counted as a LEX recovery**
   — i.e. visible, but never confused with a bot action. This would make the entry's displayed
   P&L match the account's true economics ($43.68) while still keeping OWN-01's ledger
   strictly bot-only and still keeping LEX-07 correctly unsatisfied for that side.

**Operator has ruled for form 1 (strict OWN-01) as of 2026-07-13.** Recorded here per this
document's own convention (§5a) of surfacing both readings rather than silently picking one.

**Proposed TC-OWN-12** — OWN-01a retraction: a mistakenly-backfilled operator order id is
retracted; `own_order_ids()` excludes it (including the self-referential trap above);
`corrected_value` renders the newest own-scoped correction after a fresh reconcile;
retraction changes no money fold byte-for-byte on its own (only a subsequent reconcile does);
the LEX-07 watchdog (item E) is unaffected by the retraction either way — the entry's
CALL side stays correctly un-terminal in the journal.

---

### B. PNL-01 — `config.fee_model` did not exist anywhere in the codebase

**Spec today (`spec/01-strategy-rules.md:244`):**
> **PNL-01** All P&L calculations include per-contract commissions and fees from
> `config.fee_model` (SPX has index option fees; keep the model configurable and reconcilable
> against broker statements).

`spec/06-configuration.md:169`:
> `fee_model` | per-contract fee table | tastytrade SPX schedule (verify at build time) |
> next-day | PNL-01

**Neither the config key nor any per-fill fee computation existed anywhere in the codebase
outside spec text — zero references.** `CondorFilled.fee`, `ShortStopped.fee` and
`LongSold.fee` were journaled as `0` on **every event the bot has ever written**; no
construction site passed `fee=`, so all took the dataclass default. Consequence: the bot has
journaled fee=0 on every event of its live history; every `CorrectionRecord` in the live
journal reads `fees bot=0 vs broker=<real>`.

**Built (commit `0d1825e`), `backend/src/meic/config/fee_model.py` + `domain/fees.py`:**
sourced from tastytrade's published "Commissions & Fees" schedule (fetched from tastytrade's
own asset CDN, "last updated July 1, 2026") plus its Single-Listed Exchange Proprietary Index
Options Fees table:

```
commission   $1.00/contract  — SELL-TO-OPEN only (short leg being opened); $0.00 to close
clearing     $0.10/contract  — every contract, open or close
regulatory   $0.02/contract  — every contract, open or close (ORF)
exchange     $0.60/contract  — every contract, open or close (SPX index-option fee)
```
⇒ a short-leg open costs $1.72; every other contract (long open, any close) costs $0.72. One
shared `domain/fees.py::fee_for_leg/fee_for_legs`, used by all nine construction sites
(condor fill, resting-stop close, decay buyback close, LEX long sale, watchdog escalation
close, boot-reconcile-synthesized stop, etc.) — never duplicated per call site.

**Ask the adviser to RATIFY THE TABLE VALUES** — this is what doc 06's "verify at build time"
instructs, and PNL-04's existing clause (*"systematic divergences feed corrections back into
the fee model"*) already anticipates this table needing periodic confirmation against broker
statements.

**Be honest about the verification, per item A's retraction above.** The build's own
verification claimed two independently-observed live days confirming $6.32 (2026-07-10 and
2026-07-13, both apparently 4-open + 2-close). **That claim is now half-withdrawn.**
2026-07-13 remains a genuine, independent match. **2026-07-10 is NOT** — it only looked like
a 4-open/2-close shape because the operator's order (item A) supplied the 6th contract. There
is now an **unrun, falsifiable prediction**: after the retraction, a live re-reconcile of
2026-07-10 must show own-scoped broker fees of **exactly $5.60** (gross $40.00, net $34.40),
per the arithmetic in item A. If it does not, the fee table is wrong or the retraction is
incomplete — this is a real test the adviser should ask to see run, not take on the agent's
word.

**Proposed rule — PNL-06 (Fee model ratification):** *`config.fee_model` is the table above
(commission $1.00 SELL-TO-OPEN only, clearing $0.10, ORF $0.02, SPX exchange fee $0.60, all
per contract), applied at every fill-recording site via the single `domain/fees.py` module.
Reconcilable against broker statements per PNL-04; a systematic divergence is a fee-model
correction, never a silently-absorbed rounding difference.*

**Proposed TC-PNL-04** — PNL-01/PNL-06 fee model: a 4-open/2-close scripted day matches the
table to the cent (short open 1.72 × 2, long open 0.72 × 2, close 0.72 × 2 = 6.32, the
2026-07-13 vector); the post-retraction 2026-07-10 vector (4 opens + 1 close = 5.60, gross
40.00, net 34.40) is pinned as a SEPARATE, later-added scenario once the live re-reconcile
confirms it — never asserted from the unverified prediction alone.

---

### C. DAY-03 — "today" was the OS local timezone, not ET

**Spec today (`spec/01-strategy-rules.md:17`):**
> **DAY-03** The bot operates in ET (America/New_York). All configured times are ET.
> [clock-drift verification clause follows, unaffected by this finding]

The spec says configured *times* are ET but is **silent** on what stamps the trading-day
*date* — this is the gap the bug fell through.

**Evidence, reproduced live.** `adapters/api/server.py` computed "today" as
`datetime.now(timezone.utc).astimezone().date()` — `.astimezone()` with no argument converts
to the **OS/operator machine's own local timezone**, not ET. On a BST machine at 23:53 UTC on
2026-07-13 this returned `2026-07-14`, while entry ids are stamped in ET
(`reporting/folds.py::entry_day`). **Reproduced live:** the running panel's `GET /entries`
returned **zero entries** — the day's real cert trade `2026-07-13#2` had vanished from the
operator's board, because the day-scope filter compared an ET-stamped entry id against a BST
"today". The board goes blank every evening after 7pm ET on a BST machine. `live_gates.py`
already had a correct `ET = ZoneInfo("America/New_York")` constant — the codebase knew the
right answer in one place and contradicted it in another, and the two only ever agreed **by
accident**, because during market hours BST and ET share a calendar date.

**Worse than a blank panel:** the same wrong "today" also stamps every new entry id and gates
ENT-05. For a BST operator, local midnight is 7pm ET — after the close, so trading itself
never hit the bug. **For an operator in Tokyo, local midnight is 11am ET — mid-session:**
entries fired after 11am would be stamped with tomorrow's date. Latent, timezone-dependent,
silent, and would have shipped invisibly on this exact codebase had the operator's machine
been in a different zone. A second site, `adapters/dxlink/chain_snapshot.py`, used
`date.today()` (also OS-local) to pick the 0DTE **expiration** — the same bug class aimed
directly at strike selection rather than reporting.

**Built (commit `0d1825e`):** one shared `application/market_calendar.py::trading_day(now)` /
`trading_day_str(now)`, over a single `ET` constant, always taking the **injected clock**
(never a bare `datetime.now()`), used at every site that previously derived "today" from a
clock reading. `trading_day()` refuses a naive (timezone-unaware) datetime rather than
silently guessing UTC or OS-local — the exact ambiguity that produced the bug. Instants
(`at=` fields on events) are deliberately left UTC; only the **trading day** is ET.

**Proposed rule — DAY-03a (The ET trading day is a single named concept, distinct from
instants):** *"Today" for the purposes of entry-id stamping (ENT-11(3)), day-scope filtering
(RPT-01 and every `/entries`-style query), and 0DTE expiration selection is the ET calendar
date derived from the system's injected clock via one shared function
(`trading_day`/`trading_day_str`) — never a second `ZoneInfo`/`astimezone()` call, never the
OS/operator machine's local timezone, never `date.today()`. Event `at=` timestamps remain UTC
instants; DAY-03a governs only the DATE used to group and gate, never the instant recorded.*

**Proposed TC-DAY-08** — DAY-03a: an entry fired with the system clock reading a UTC instant
whose OS-local conversion names a DIFFERENT calendar date than ET (e.g. a BST clock reading
23:53 UTC, or a simulated Tokyo-zone clock at what is 11am ET) is stamped with the ET date in
both its entry id and every day-scope filter; 0DTE expiration selection likewise uses the ET
date, never the OS-local one. Reproduces the exact 2026-07-13 blank-panel live incident as a
regression vector.

---

### D. EOD-01 — settlements were never captured, and sides were never marked EXPIRED

**Spec today (`spec/01-strategy-rules.md:189`):**
> **EOD-01** Default: all remaining positions are held to expiration. SPX 0DTE is
> cash-settled to SET/closing value; no exercise/assignment handling is required. After
> settlement, the bot marks all remaining sides `EXPIRED`. **Settlement cash is
> BROKER-JOURNALED, never merely computed (v1.59)** [...] each expiring position's settlement
> cash is sourced from the broker's Receive-Deliver/settlement transaction records into a
> journaled settlement event [...] Until the broker settlement record is captured, an
> ITM-expiring position's P&L is PROVISIONAL and labeled so. Idempotent: re-runs never
> duplicate settlement records [...]

**Reality found in the live journal: ZERO `SettlementRecorded` events, ever**, across the
entire history, and `SideExpired` is emitted by **nothing in the live path** — the only
emitter in the whole codebase was the demo simulator (`composition/runtime.py`). Every entry
the bot has ever traded is permanently stuck `settlement_pending=True`, never reaching a
terminal state.

**Root cause, a trap that guarantees 100% loss of settlements (commit `52b83de`):**
1. At 16:15 ET the EOD pass captures settlements for TODAY.
2. SPX 0DTE settlements post the broker's Receive-Deliver ledger the **next business day** —
   the adapter's own `end_date = day + 1` fetch window exists for exactly this reason.
3. So at 16:15 there is nothing there yet ⇒ the capture appends nothing.
4. `reconcile_day(day)` then writes `DayBrokerConfirmed` / an own-scoped `CorrectionRecord`
   anyway (the day's *cash* reconciled fine from trade rows alone).
5. The "already reconciled" gate now blocks that day **forever**. Nothing ever looks back.
6. The settlement, which posts tomorrow, is **never captured** — `capture_settlements`'s own
   docstring says it is explicitly designed to be re-run; nothing ever ran it again.

**Fix — bounded look-back (commit `52b83de`):** `MEIC_SETTLEMENT_LOOKBACK_DAYS` (1–30, default
5) — each EOD pass also walks the recent trading days that still have `settlement_pending`
entries (a cheap, log-only check, no broker call for a day with nothing outstanding) and
re-calls the same, unchanged `capture_settlements`. A day whose look-back capture actually
appends a NEW settlement row is re-reconciled, deliberately bypassing the "already" gate for
that one case (the gate exists to stop redundant reconciles of unchanged facts, not to freeze
a day whose facts just arrived). Per-day try/except: one day's broker failure never blocks
another's, nor crashes the tick.

**Fix — live-path `SideExpired` (commit `9567a9b`):** `_mark_expired_sides(events, day)`, run
after settlement capture, marks a side `SideExpired` iff it is (1) remaining — not stopped,
not closed, entry not closed by any other path; (2) **settled** — its short leg's symbol has
an actual `SettlementRecorded` in the log, the exact inverse of the existing
`settlement_pending` predicate, **never a guess from a clock or computed moneyness**; (3) not
already marked (idempotent). Deliberately not filtered by ITM/OTM — EOD-01 marks ALL remaining
sides EXPIRED, and an ITM cash-settlement's effect is already carried in the settlement row
itself.

**Proposed rule — EOD-06 (Settlement look-back capture):** *`capture_settlements` MUST be
re-attempted for recent prior trading days that still hold an uncaptured settlement, bounded
by `config.settlement_lookback_days` (1–30, default 5) — never only for "today". A day whose
look-back capture newly appends a settlement row is re-reconciled against broker truth
(PNL-04), independent of any "already reconciled" gate, which governs only the case where
nothing changed.* Note for the adviser: **the spec never said the capture must look back**,
and that omission is exactly what made 100% settlement loss possible — EOD-01's "after
settlement" clause presumed same-day posting, which SPX 0DTE settlements do not do.

**Proposed rule — EOD-07 (Live-path side expiry, from broker truth only):** *A remaining side
(not stopped, not closed) is marked `SideExpired` if and only if its short leg's symbol has an
actual, broker-journaled `SettlementRecorded` — never inferred from the clock, from computed
moneyness, or from the passage of time alone. This closes the gap where `SideExpired` was
emitted by the demo simulator only and never by the live path.*

**Proposed config addition to doc 06:** `settlement_lookback_days` | 1–30 | 5 | immediate |
EOD-06.

**Proposed TC-EOD-06** — EOD-06 look-back: a day sealed with `DayBrokerConfirmed` before its
settlement posted is re-captured on a later tick once the broker's Receive-Deliver row
appears, and re-reconciled; idempotent on a second pass; bounded (a day older than
`lookback_days` is never re-fetched); one day's broker failure never blocks another's.

**Proposed TC-EOD-07** — EOD-07 live-path expiry: an untouched condor whose four legs all get
`SettlementRecorded` reaches `EntryProjection.status == "EXPIRED"` on both sides; a
stopped/LEX'd/decay-closed/operator-closed side is never marked expired even if its symbol
later settles; a short with no settlement row is never marked expired regardless of elapsed
time; idempotent across repeated passes.

---

### E. LEX-07 — a ladder-start invariant, and the DCY-03 exception

**Spec today (`spec/01-strategy-rules.md:142`):**
> **LEX-07** The long is **always sold** — there is no keep-cheap-longs threshold. Every
> stopped side fully closes; per-side position state after LEX completes is flat.

**Spec today, the exception (`spec/01-strategy-rules.md:184`):**
> **DCY-03 Leftover long.** After a decay buyback the side's long is **left to expire** — it
> is further OTM than the dead short, its bid is ~zero, and it acts as a free hedge. [...]
> LEX-07's always-sell applies to stop-outs, where the long has real value — not here.

**Evidence.** On 2026-07-10 the bot journaled `ShortStopped(entry_id="2026-07-10#1",
side="CALL")` and then **nothing** — no `LongSaleStarted`, no `LexOrderPlaced`, no
`LongSold`, no `SideClosed`. The LEX ladder never ran (live stop-fill detection did not exist
yet that day). **This went unnoticed for three days.** Not the tests (they pass against
harnesses the live composition never runs). Not the EOD reconcile — the day total is
broker-derived (PNL-04), so it came out numerically right anyway and *masked* the gap. Not the
dashboard. It took a manual read of the raw journal to find. Nothing in the system checks that
a ladder *started* — every existing guard checks that something which happened was correct;
none checked for an absence.

**Built (commit `4acc5bf`), `application/lex_ladder_watchdog.py`:** a pure, journal-driven
fold — once a side is `ShortStopped`, a `LongSaleStarted` for that (entry, side) MUST follow
within `config.lex_ladder_grace_seconds` (10–300, default 60) or a CRITICAL alert names the
entry and side. Journal-driven **deliberately**, not a hook inside the LEX code path, so it
still fires when the LEX service is unwired, unreachable, or dead — a hook inside a component
that never runs, never runs either. `ShortStopped.initiator == "decay"` (DCY-03) is checked
directly as a legal exception, not inferred from a later `EntryClosed`, because the decay path
appends those as two separate list entries and a crash between them must not false-alarm on
otherwise-correct behaviour. One alert ever per (entry, side).

**Proposed rule — LEX-10 (Ladder-start invariant watchdog):** *Once a side is journaled
`ShortStopped` with an initiator other than `"decay"` (DCY-03's exception), a `LongSaleStarted`
for that (entry_id, side) MUST be journaled within `config.lex_ladder_grace_seconds` (10–300,
default 60) or the system raises one CRITICAL alert naming the entry and side. The check is
purely a fold over the event log — it must operate correctly even when the LEX service itself
is unwired, unreachable, or crashed, since that is precisely the failure class it exists to
detect.*

**Proposed config addition to doc 06:** `lex_ladder_grace_seconds` | 10–300 | 60 | immediate |
LEX-10.

**Proposed TC-LEX-11** — LEX-10 ladder-start invariant: a genuine stop-out with no
`LongSaleStarted` at all alerts CRITICAL once past the grace window, naming entry+side; a
fully-journaled ladder (start → reprice → sold → closed) never alerts; a DCY-03 decay-initiated
stop with no ladder never alerts, even alone without its paired `EntryClosed` (crash-between
-appends case); one alert ever per (entry, side) across repeated ticks; two sides of the same
whipsawed entry (STP-08) are tracked independently.

---

### F. ORD-09 — the OWN-09/10 standdown journals nothing (still open)

**Spec today, the philosophy this extends (`spec/01-strategy-rules.md:84`, `189`):**
> **ORD-09** [...] the broker-reported instrument symbol [...] and the broker-allocated fill
> price, exactly as returned by the broker [...] *(ORD-09 philosophy, quoted elsewhere in the
> spec: "record what the counterparty says happened" / "an unjournaled order is unauditable")*

**Spec today, OWN-09/10 (`spec/01-strategy-rules.md:236–237`):** *"the bot never fixes the
consequences of manual interventions"* — the operator's own order cleanup after a manual
intervention, zero order actions by the bot.

**Evidence, code (`application/stop_fill_watch.py:506–513`):** when a stop fill is caught up
late (EC-STP-06) and the long is discovered no longer held at the broker, the bot logs:

> `"EC-STP-06 catch-up: the short's stop had already filled, but the long was no longer held
> at the broker -- standing down (operator disposed of it directly, OWN-09/10). No LEX order
> submitted."` (level `info`)

**and appends no event at all.** Two consequences, both real, both observed on 2026-07-13:

1. **A journal-driven detector is structurally blind to it.** The new LEX-07 watchdog (item E)
   folds over `ShortStopped` / `LongSaleStarted` / `LongSold` / `SideClosed` / `EntryClosed`
   only. An OWN standdown leaves the (entry, side) key permanently in the watchdog's "pending"
   set — nothing marks it terminal — so **the very watchdog just built to catch a silent LEX
   failure will itself raise a false CRITICAL alert on every future occurrence of this exact,
   already-correctly-handled case**, once `lex_ladder_grace_seconds` (60 s default) elapses
   after the standdown. This is a direct, code-level interaction between items E and F, not
   speculation: `_pending_ladder_starts` (item E) has no branch for a standdown at all.
2. **The message reads as benign** — "the operator did it, fine" — when what it actually means
   is "the bot failed to sell the long and a human had to rescue the trade." This is the exact
   incident in item A: **it masked a real LEX-07 failure**, and the author of the original
   §4b/§5 sections of this document read a version of this log line, believed it, and nearly
   suppressed the watchdog build that went on to correctly catch the underlying bug.

**Proposed rule — ORD-11 (Standdown journaling, extending the ORD-09 principle):** *Every OWN
standdown (the bot declines to act because the position it would act on is no longer held, per
OWN-09/10's zero-touch principle) MUST append a journaled, metadata-only event carrying its
entry, side, and reason — `OwnDisposalStanddown(entry_id, side, reason, at)` or equivalent —
folded into no P&L projection. This event is a legal terminal state for the LEX-07 watchdog
(item E), the same way `ShortStopped(initiator="decay")` is DCY-03's legal exception: an
unjournaled decision is unauditable (ORD-09), and in this specific case, unauditable also means
indistinguishable — to every existing detector — from the exact failure those detectors exist
to catch.*

**This is NOT YET IMPLEMENTED.** Flagged here as a proposed rule awaiting ratification AND
build — unlike items A–E, there is no commit to cite for it. Given its direct interaction with
the just-shipped LEX-07 watchdog (a live false-CRITICAL-alert risk on the very next occurrence
of this standdown path), the agent recommends the adviser treat this as higher priority than
its "still open" framing might otherwise suggest.

**Proposed TC-ORD-09** — ORD-11 standdown journaling: an EC-STP-06 catch-up that discovers the
long no longer held appends the standdown event (never a `LongSold`, never a `SideClosed` —
the bot did not sell it); the LEX-07 watchdog (item E) treats a standdown-marked (entry, side)
as legally terminal and never alerts on it, mirroring the existing DCY-03 exception test.

---

### G. The systemic finding — for the adviser, not just the reviewer

**Six components were built, unit-tested, and never wired into the live composition:**
NFR-04 QuoteHub, the STP-03b stop watchdog, EC-STP-08 stop-limit escalation (**confirmed
STILL DEAD as of this writing** — `application/stop_escalation.py` exists and is unit-tested,
but is referenced by nothing in `adapters/api/server.py` or any `composition/*.py` file — grep
confirms zero wiring sites), live-path `SideExpired` (item D), settlement capture look-back
(item D), and the OWN-09/10 standdown journaling (item F, not yet built at all). **Every one
had passing tests — against harnesses the live composition never ran.** As the LEX-07 commit
(`4acc5bf`) puts it: *"a component that is never invoked emits no WRONG events — it emits NO
events, and nothing that inspects events for mistakes can see it."*

**Proposed rule — NFR-07 (Live-composition wiring audit):** *Every component the spec mandates
be part of the live trading path MUST be provably constructed AND ticking inside `live_app()`
(or its equivalent live composition root) — not merely unit-tested against a harness the live
path never runs. Enforced by a dedicated wiring-audit test per component, in the spirit of the
existing traceability gate (`scripts/check_traceability.py`), which counts rule-to-test
coverage but has no way to tell whether the code a passing test covers is ever reached by the
running system.* Precedent already exists in this codebase for the pattern (e.g.
`test_live_app_wires_a_real_stop_watchdog_task_with_env_thresholds`,
`test_live_app_wires_a_real_lex_ladder_watchdog_task_with_env_threshold`) — NFR-07 would make
one such test **mandatory for every live-mandated component**, not an ad hoc habit the agent
happens to have picked up after the fifth incident.

**Proposed TC-NFR-07** — NFR-07 wiring audit: for each component named in a fixed manifest
(QuoteHub, stop watchdog, stop-limit escalation, EOD settlement look-back, live-path
`SideExpired` marking, LEX-07 ladder watchdog, and any future addition to the manifest),
`live_app()` constructs a real instance and starts its background task/wires its call site;
a component present in the manifest but absent from `live_app()`'s startup sequence fails the
test by name, not by silent omission — the fix this document has now applied to the same bug,
five separate times, as an architecture-level backstop rather than a recurring manual audit.
