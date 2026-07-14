# Review pack — 2026-07-13/14 hardening batch

**To:** the human reviewer of the MEIC bot (real-money, tastytrade production, SPX 0DTE
iron condor).
**From:** coding agent, self-audit of `live-testing-2026-07-09` since the last review.
**Purpose:** tell you where to be suspicious. This is not a changelog and not a
demonstration of work done — several of the findings below are the agent's own
mistakes, stated plainly so you don't have to re-derive them.

Every commit sha, file path, line reference, and number in this document was checked
against the actual repository state on 2026-07-14 (`git show`, `grep`, and running the
project's own scripts under `.venv`). Where a claim in the agent's own commit messages
turned out to be wrong, that is flagged explicitly rather than silently corrected.

---

## 1. The headline finding — read this even if you read nothing else

Six components were built, unit-tested, and **never wired into the live composition**.
Their tests passed. The tests passed because they exercised the component directly —
never through `live_app()`, the thing that actually runs in production. A component
that is never constructed and never ticked emits no events at all, which means nothing
that inspects the event log for mistakes can ever see the gap. This is not six
unrelated bugs. It is one hole, found six times.

| # | Component | Status | Fix commit |
|---|---|---|---|
| 1 | NFR-04 QuoteHub (live DXLink marks) | fixed | `f0335d8` |
| 2 | STP-03b stop watchdog (second protection layer) | fixed | `ec61fe0` |
| 3 | EC-STP-08 stop-limit escalation | **still dead code** | none |
| 4 | Live-path `SideExpired` (only the demo simulator emitted it) | fixed | `9567a9b` |
| 5 | Settlement capture (sealed the day before settlement existed) | fixed | `52b83de` |
| 6 | OWN-09/10 standdown alert | **journals nothing, still open** | none |

Verified for #3: `backend/src/meic/application/stop_escalation.py` defines
`requires_unfilled_watchdog()` and `should_escalate_to_market()`. A repo-wide grep
for both names finds exactly two hits: the module itself and
`tests/application/test_tc_stp_09_ord_04.py`. Nothing in `composition/live.py` or
`adapters/api/server.py` calls either function. The `stop_order_type` config key
(`spec/06-configuration.md` line 63, default `stop_market`, alternative `stop_limit`)
is likewise read nowhere outside that dead module — and separately,
`application/order_intent.py`'s stop-order builder (`protective_stop`, line 149)
**hardcodes `order_type="stop_market"` unconditionally**, so even if something did
read the config key, there is no code path that would ever construct a `stop_limit`
order. Section 7 asks the reviewer to rule on stop_market vs stop_limit; be aware
going in that stop_limit is not "implemented but paused" in any meaningful sense —
the order-construction code for it does not exist.

Verified for #6: `application/stop_fill_watch.py:508-511` — the standdown path calls
only `_alert_once(comp, alerts, ..., "info", ...)`. No event is appended to the
journal. Section 4 explains why this specific gap is dangerous, not just incomplete.

**Recommendation:** do not spend review time re-verifying these six individually —
they are checked below and each has a passing regression test written against the
real live composition (`test_live_app.py`, `test_watchdog_wiring.py`,
`test_eod_reconcile_trigger.py`). Spend it looking for a **seventh**. The durable fix
is not "check more carefully" — it is a wiring-audit test that walks the spec's
component list and asserts each one is provably constructed and ticked inside
`live_app()`. That test does not exist yet.

---

## 2. What was actually broken, and how it hid

| Commit | Rule(s) | What was broken | Why nothing caught it |
|---|---|---|---|
| `56d99e1` | OWN-01/OWN-03, PNL-04 | RPT-15 reconciled the **entire shared brokerage account** as if every fill were the bot's. On 2026-07-10 it journaled a `CorrectionRecord` claiming broker truth was **−$534.46** on a trade that made **+$43.68**. The account is shared with the operator's own trading (v1.49) — that is first-class, not an edge case — and the bot had never journaled its own entry order id, so the reconciler had no way to scope. | No test constructed a shared account with a foreign trade in it; every fixture was a clean single-trade day. |
| `34b3165` | reporting, ENT-05 | `day_report(events)` folded the **whole log**, not just today — a manual fire believed 2 entries had filled today when 1 had. Because `manual_entry._filled_today()` reads `day_report(...).entries_filled` for the ENT-05 trading gate, this could have **permanently blocked manual entries** once the lifetime total passed the daily cap. The scheduled path (`run_day()`) kept its own local counter and was unaffected — which is exactly why it went unnoticed. | Every test fixture had entries from only one day; nothing exercised a log spanning two days. |
| `5827251` | UI-13 | Live P&L re-marked **all four legs** of a condor including already-CLOSED sides, pricing a spread the bot no longer owned. 2026-07-13#2 showed `live_pnl = -72.50` while the TPF/TPT evaluator (already correct) showed `+14.3%` at the same instant. The 2026-07-10 card showed `-$387 / "Profit -161.1%"` by re-marking a spread that had expired a week earlier. | The TPF/TPT evaluator and the display used two independent formulas; only the evaluator was tested against a closed-side scenario. Exits never misfired — only the number the operator was watching lied. |
| `ec61fe0` | STP-03b | The stop watchdog (second protection layer, behind the broker's own resting stop) was fully written and unit-tested and never constructed in `live_app()`. | See §1. |
| `52b83de` | EOD-01 | **Zero** `SettlementRecorded` events exist anywhere in the live journal's history. The EOD pass captured settlements for *today* at 16:15 ET, but SPX 0DTE settlements post the **next business day**; nothing existed yet at capture time, and an `already`-reconciled gate then sealed the day forever, even though `capture_settlements`'s own docstring says it is designed to be re-run. Confirmed: `capture_settlements` had no caller anywhere except the same-day EOD pass before this fix. | The `already` gate (itself tightened the same day, in `56d99e1`, to stop legacy corrections counting as reconciled) had the side effect of also blocking a legitimate re-run. Fixing one thing exposed the next. |
| `9567a9b` | EOD-01 | The **live path never emitted `SideExpired`**. The only emitter in the whole codebase was the demo simulator (`composition/runtime.py:96`). A condor finishing between the shorts — the most common, most desirable MEIC outcome — fell through `EntryProjection.status` to `PROTECTED` forever, with a live Close button rendered on a position that no longer existed. | The demo path and the live path never shared this piece of logic; the live regression suite tested against harnesses the live composition never ran. |
| `0d1825e` | PNL-01, DAY-03 | Two fixes bundled (both touch `server.py`, suite only green with both). **PNL-01:** `config.fee_model` is mandated by the spec (`06-configuration.md` line 169) and **did not exist anywhere in the codebase**. Every `CondorFilled`/`ShortStopped`/`LongSold` ever journaled recorded `fee=0` — confirmed: every `CorrectionRecord` in the live journal reads `fees bot=0`. **DAY-03:** "today" was computed via `datetime.now(timezone.utc).astimezone().date()` with no argument, i.e. the **OS's local timezone**, not ET. Reproduced live: on a BST machine after 19:00 ET the panel's `/entries` returned **zero entries** because an ET-stamped entry id was compared against a BST "today". Worse, `commands.day()` also stamps new entry ids and gates ENT-05 — an operator in a timezone ahead of ET (e.g. Tokyo, UTC+9) would have entries **stamped with tomorrow's date mid-session**. `dxlink/chain_snapshot.py` separately used `date.today()` (OS local) to select the 0DTE **expiration** to trade. | During UK market hours BST and ET happen to share the same calendar date, so the DAY-03 bug only manifested after the US close — exactly when nobody was watching the panel. |
| `4acc5bf` | LEX-07 | New watchdog for a stop-out whose long is never resold. See §4 — this is the fix that surfaced the 07-10 incident's true shape. | n/a (new detector, not a fix for prior silence) |
| `1436da4` | OWN-01 | Retraction of an order the agent had mislabelled as the bot's own inside the *very* order-id backfill meant to fix shared-account leaks. See §5. | n/a |

---

## 3. VERIFIED vs MERELY TESTED

**VERIFIED against the live broker, the live journal, or by reproducing the reported
symptom in the running app** (not just a green unit test):

- Own-scoping (`56d99e1`): cert-day reconcile matched broker truth to the cent —
  cash_delta bot=50.00, broker net 43.68 after fees 6.32, scope=`"own"`.
- STP-02b buffer reaches the broker: `stop_trigger = floor_to_tick(0.95 × net_credit +
  markup)`. Traced against real production code and a real 2026-07-13 vector: credit
  2.80, markup 0.30 → raw 2.96 → floored to **2.95** (confirmed by independent
  recomputation, not just the commit message). Zero-markup control on the same credit
  gives 2.90 (0.95 × 2.80 floored). The buffer provably reaches the broker.
- Settlement capture (`52b83de`): on restart it immediately captured 2026-07-10's
  put-spread expiry, which had been permanently unreachable before the fix.
- `SideExpired` (`9567a9b`): 2026-07-10 now reads PUT expired / CALL stopped, matching
  the operator's own account of that day.
- DAY-03 (`0d1825e`): the panel's `/entries` was reproduced returning 0 entries live,
  and confirmed fixed after the patch.
- LEX-07 watchdog (`4acc5bf`): fired CRITICAL, unprompted, identifying the real
  2026-07-10 incident from the journal alone.
- Test counts and gates, run directly against the repo on 2026-07-14 (not taken on
  faith from commit messages):
  - `pytest -q -m "not contract"` → **1440 passed, 13 deselected** (exact match to
    the claim in recent commit messages; note the plain `pytest -q` in
    `CLAUDE.md`'s command list will fail to collect in this environment unless
    `pytest_bdd` is installed — use `.venv`, not the system Python).
  - `scripts/verify_spec_lock.py` → **17 files intact**.
  - `scripts/check_traceability.py` → **220 rules covered, 152 test cases**.
  - `logs/` directory (via `.gitignore`, which documents this itself): earliest
    files are all timestamped **2026-07-13**; nothing from cert day (07-09/07-10)
    exists. Server logs were not written at all until 2026-07-13 — this is why
    several of the bugs above went unnoticed for days; there was no record to
    notice them in.

**TESTED ONLY — not verified against the broker:**

- **PNL-01 fee model.** State the over-claim plainly: commit `0d1825e`'s own message
  asserts the fee model (commission $1.00/contract + $0.10 clearing + $0.02 ORF +
  $0.60 exchange per side) was verified against "TWO independently observed live
  days" (2026-07-10 and 2026-07-13), both allegedly a 4-open/2-close shape summing to
  exactly $6.32. **This claim is wrong, and commit `1436da4` says so itself:**
  2026-07-10 only looked like a 4-open/2-close shape because the sixth contract
  (the fee-relevant "close" leg) was the **operator's own manual order**, wrongly
  attributed to the bot (see §5). Strip that order and 2026-07-10 becomes a
  **4-open/1-close** shape the fee model was never fitted against:
  `2×1.72 (short opens) + 2×0.72 (long opens) + 1×0.72 (one close) = 5.60`
  (gross $40.00, net $34.40). Only 2026-07-13 is a genuine independent verification.
  **The reviewer should run the corrected 07-10 reconcile and confirm the broker's
  own-scoped fee comes to exactly $5.60.** If it doesn't, the fee model is wrong and
  should be pulled from the live path until refitted.
- The buffer's calibration (§7) — the mechanism is verified to reach the broker, but
  whether its dollar amount is well-calibrated to actual long-recovery cost is not.

**NOT VERIFIED AT ALL:**

- EC-STP-08 escalation — dead code, never runs in any environment, confirmed above.
- `stop_limit` order type — no construction path exists for it at all (see §1); it is
  not merely "paused."

---

## 4. The 2026-07-10 incident, in full

Sequence, reconstructed from the journal and the commit history's own timestamps:

1. The CALL short stopped at the broker (order `482621556`, ~11:56 ET per `6e70603`'s
   own commit message).
2. The bot had **no live stop-fill detection** at that moment — it was added later
   that same day, commit `6e70603` (timestamped 2026-07-10 23:20 local), well after
   the market close.
3. The bot never noticed its own stop had filled. No `LongSaleStarted`, no
   `LexOrderPlaced`, no `LongSold`, no `SideClosed` — nothing. The LEX ladder never
   ran.
4. **LEX-07 ("the long is always sold after a stop-out") was silently violated.**
5. The operator watched the position, correctly judged the bot was not going to act,
   and sold the long call himself from his own tastytrade platform (order
   `482760202`, SPXW 260710C07595000).
6. Days later, the bot's catch-up logic (`stop_fill_watch.py`) went looking for the
   long, found it gone, and journaled — via `_alert_once`, an **info-level, unjournaled
   alert only** — `"EC-STP-06 catch-up: the short's stop had already filled, but the
   long was no longer held at the broker — standing down (operator disposed of it
   directly, OWN-09/10). No LEX order submitted."`

That message reads as benign. **It is the bot rationalising its own failure.** The
OWN-09/10 standdown alert actively masks a LEX-07 violation, and because the standdown
writes no event to the journal (confirmed in §1 — only `_alert_once`, no
`SideClosed`/no equivalent), any future journal-driven detector is structurally blind
to it: it sees an entry that reached a clean terminal state with no visible gap, not
a bot that failed to do its job. The agent that wrote this document initially read
that alert, believed its framing, and nearly reported the LEX-07 watchdog (§1 item 6's
sibling detector, `4acc5bf`) as a false alarm before re-examining the raw event log.
This is the single most instructive thing in this pack: **the system's own
explanatory text for its own failure was more convincing than the failure itself.**

---

## 5. Where the author (this agent) got it wrong

Listed without softening, because a reviewer who catches one of these unstated will
rightly distrust everything else here.

- **Invented a market scenario to explain a wrong number.** On first seeing the
  reconciler's −$534.46, the agent trusted it as broker truth and reverse-engineered
  a fictional scenario (SPX settling in-the-money) to explain it. The operator
  corrected this: SPX closed 7575.26 and never came back; the put spread expired
  worthless; the trade made money. **A fabricated "broker truth" is more dangerous
  than none — it gets believed.** This is recorded in the operator's own amendment
  proposal (`AMENDMENT-PROPOSAL-2026-07-12-rpt15-own-scoping.md`, §0) as the reason
  the whole own-scoping rule set exists.
- **Inferred an unprotected short from a missing transaction row.** A resting stop
  that never triggers produces no transaction. Its absence proves the stop was never
  hit, not that it didn't exist. Retracted in the same amendment document (§5a).
- **Told an implementer that legs' broker/OCC symbols were DXLink-subscribable.**
  They are not — DXLink speaks streamer symbols, not OCC symbols. The subscription
  matched nothing, the QuoteHub stayed empty, and the fallback silently served the
  old chain-snapshot mid. Tests stayed green throughout (`f0335d8`'s own commit
  message: "green tests, deployed, zero effect").
- **The first prescribed fix for that bug was also wrong** — translating via
  `ChainSnapshot.symbols` would have been another silent no-op, because that map
  also holds OCC symbols, not streamer symbols. The implementer caught this, not the
  agent that specified the fix.
- **Mislabelled the operator's own manual order as the bot's.** In the RPT-16
  order-id backfill, the agent wrote `OwnOrderIdBackfilled(..., broker_order_id=
  "482760202", role="lex")` — assuming the order that closed the orphaned long
  (§4, step 5) was the bot's LEX order. It was the operator's own manual trade. This
  reintroduced the exact shared-account leak (OWN-01/OWN-03) that the whole
  own-scoping fix (`56d99e1`) existed to close — **inside the fix for shared-account
  leaks itself.** Retracted in `1436da4`, which also notes the retraction had to
  specifically guard against a naive `own_order_ids()` implementation adding the id
  right back in, because the retraction event itself carries a `broker_order_id`
  field.
- **Over-claimed the fee model's independent verification** — see §3.
- **Called the LEX-07 watchdog's first real alert a false alarm**, on the strength of
  the OWN-09/10 standdown message's framing — see §4.

The pattern across every one of these: **treating a plausible inference as an
observation.** The spec's own rule, PNL-04 ("the broker-derived figure is what the
report presents as authoritative — broker truth wins every dispute"), is the direct
antidote, and it was violated repeatedly by the same agent that implemented it.

---

## 6. Open items, ranked

1. **OWN-09/10 standdown journals nothing.** ORD-09 states "an unjournaled decision
   is unauditable." This is not abstract — it masked a real LEX-07 failure for three
   days (§4). Highest priority open item.
2. **EC-STP-06 catch-up re-evaluates entries that never reach a terminal state, on
   every tick.** Because of the settlement-capture bug (fixed `52b83de`), entries
   that predate the fix could linger open indefinitely; `34b3165`'s commit message
   flags this explicitly as "not fixed here, still open" — 2026-07-10#1 lingered in
   `fold().entries` for days because its settlement was never captured. Scope is now
   narrower post-fix, but the catch-up scan has no age cutoff of its own — it relies
   entirely on entries eventually closing.
3. **EC-STP-08 escalation is still dead code** — confirmed in §1.
4. **`EntryCompleted` is emitted by nothing, not even the demo simulator.**
   Confirmed: the only production reference to the constructor `EntryCompleted(...)`
   in a non-test file is none — it exists only in `domain/events.py` (the class
   definition), `domain/projection.py` and `reporting/periods.py` (both consumers of
   the event, never producers). `EntryProjection.completed` is permanently `False`.
   Currently harmless per `9567a9b`'s own note (the fold's `_settled()` check covers
   every real path via other OR-clauses), but it is the same class of gap as the six
   in §1.
5. **Domain events mostly carry no timestamp.** Correction to the framing given for
   this task: it is not universally true — `CondorFilled`, `OwnOrderIdBackfilled`,
   `OwnOrderIdRetracted`, `DayBrokerConfirmed`, `CorrectionRecord`, `EntryMarkSample`,
   `ExternalFillImported` and `SettlementRecorded` all carry an `at:` field. But the
   core stop/LEX lifecycle events do not: `StopPlaced`, `StopReplaced`,
   `StopConfirmed`, `ShortStopped`, `LongSold`, `SideClosed`, `SideExpired`,
   `EntryClosed`, `LongSaleStarted`, `LexOrderPlaced`, `WatchdogEscalated` — none of
   these have a timestamp field. This is exactly why the LEX-07 watchdog (`4acc5bf`)
   had to track wall-clock first-sighting time itself instead of reading it off the
   event; a process restart resets that clock.
6. **Server logs were not written at all until 2026-07-13.** Confirmed via
   `.gitignore`'s own comment and the `logs/` directory's file timestamps (§3).
   Cert-day logs (07-09/07-10) are permanently lost. This is the root reason several
   of the bugs in §2 survived for days undetected.
7. **`"adopted"` mislabels zero-quantity positions.** Confirmed in
   `domain/ownership.py::classify` (line 56-59): if the ledger's own tracked
   quantity for a symbol is 0 and the broker also reports 0 for it, the position is
   classified `OWNED` (not `FOREIGN`) and lands in `reconcile_boot.py`'s `adopted`
   list. This is cosmetic — the bot takes no action on a genuinely flat position —
   but it means a boot log showing a pile of "adopted" symbols may include ones the
   bot never actually holds, which reads as more alarming than it is.

---

## 7. Questions for the reviewer to rule on

- Should stops trigger off the bid rather than the mark? (The operator has parked
  this question — it is not resolved either way.)
- `stop_market` vs `stop_limit`: per §1, this is not a paused feature with working
  code behind a flag — there is no code path that constructs a `stop_limit` order at
  all, and the escalation watchdog that would be mandatory alongside it (EC-STP-08)
  is dead. What is the actual risk trade for a 0DTE SPX stop, and is `stop_limit`
  worth building at all given SPX's typical liquidity?
- **The long-recovery buffer (STP-02b) is a fixed dollar amount, so its effective
  percentage scales inversely with the entry's credit.** Verified by direct
  computation: 95% + $0.30 markup on a $2.80 credit floors to a trigger of $2.95 —
  an effective **105.4%** stop (2.95/2.80). The same $0.30 on a $2.00 credit would
  floor to $2.20 — an effective **110%** stop. Should the buffer instead be a
  percentage of credit? The amendment proposal (§4b) separately notes the only
  observed real long-recovery cost so far was $0.10 against a $0.30 buffer — the
  buffer may currently be miscalibrated in the *other* direction (too generous), but
  the mechanism has not been tested across enough real days to know. The operator
  should not discover the wrong direction by having a small-credit entry stop out
  later than expected.
- **When the operator closes a bot leg out-of-band from his own platform, whose P&L
  is it?** The operator has ruled strict OWN-01 (`1436da4`: "the bot's ledger
  contains only the bot's own orders") — meaning the $10 long-recovery value from the
  07-10 incident is permanently absent from the bot's own ledger, by design, because
  attributing it to the bot would fabricate a `LongSold` the bot never executed. This
  ruling needs to be written into `spec/` (it currently exists only as a commit
  message and this document), or a future "fix" will silently reverse it.

---

## 8. How to review this

1. `.venv/Scripts/python.exe -m pytest -q -m "not contract"` — expect **1440
   passed, 13 deselected**. (The plain `pytest -q` from `CLAUDE.md` will fail to
   collect ~65 BDD test modules with `ModuleNotFoundError: No module named
   'pytest_bdd'` unless invoked through the project's `.venv` — this is an
   environment-activation detail, not a code defect, but it will look like a
   catastrophic regression if you run it with the wrong interpreter.)
2. `.venv/Scripts/python.exe scripts/verify_spec_lock.py` — expect **17 files
   intact**.
3. `.venv/Scripts/python.exe scripts/check_traceability.py` — expect **220 rules
   covered, 152 test cases**.
4. Run the corrected 2026-07-10 reconcile prediction from §3 (fee model
   falsification) — the own-scoped broker fee for that day, with the operator's
   order stripped, must come to exactly **$5.60**. If it doesn't, pull the fee model.
5. Hunt for a **seventh** unwired component. Six were found this way; there is no
   reason to believe six is the total. The check to build is a wiring-audit test
   that asserts every spec-required component is provably constructed and ticked in
   `live_app()` — not present today.

---

## Corrections to the author's brief

The task brief this document was written against contained a small number of
overstated or imprecise claims, caught during verification:

- **"Domain events carry no timestamp"** is too broad — eight event classes do carry
  an `at:` field (see §6 item 5). The load-bearing fact is narrower: the *stop/LEX
  lifecycle* events carry none, which is what actually forced the LEX-07 watchdog to
  track wall-clock time itself.
- **"95% + $0.30 on a $2.80 credit is an effective 105.4% stop"** is correct only
  after tick-flooring is applied (raw 0.95×2.80+0.30 = 2.96 → floored to 2.95;
  2.95/2.80 = 1.0536). The raw pre-floor figure is 105.7%, not 105.4% — a detail
  worth having straight since the floor operation is itself part of what makes the
  buffer's real-world bite depend on where the credit happens to land relative to
  the tick grid, not just on the credit's size.
- Everything else in the original brief (the six-item headline, all eleven commit
  descriptions in §2, the 07-10 incident sequence, the five self-corrections in §5,
  and the test/lock/traceability counts) checked out exactly against the repository
  and is reported above without alteration.
