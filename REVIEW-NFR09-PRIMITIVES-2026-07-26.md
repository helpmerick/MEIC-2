# Adviser Review — NFR-09 primitive audit, verified against code and the pinned SDK

**Date:** 2026-07-26
**Tree:** `main` @ `8cc171d` (v1.93; STP-04a `07fc3f9` shipped)
**SDK:** `tastytrade==13.0.0` (the exact pin in `requirements-live.txt`), source inspected
**Status:** review + **proposed** amendment text. **I did not edit `spec/`.** Only the operator edits the spec; everything in §4 is text to ratify, reject, or rewrite.

---

## 0. Method, and a correction to my own last message

Per the v1.93 process rule, I re-read the current files rather than reasoning from memory: `adapters/tastytrade/adapter.py`, `adapters/sim/simulated_broker.py`, `application/ports.py`, `application/execute_entry.py`, all 14 `fills_since` call sites, and the `tastytrade==13.0.0` source itself.

**First, a correction I owe.** In my previous message I said `find_matching_order` returning `None` on an idempotency check would cause a **re-submit and a duplicate order**, and I ranked it the most dangerous item in the audit. **That is wrong.** `_recover_first_submit` (execute_entry.py:706) returns `None` on a negative, the caller **re-raises `submit_exc` unchanged and takes the clean-skip path**. There is no resubmit anywhere on that path.

The true failure on a false negative is the opposite and, in this codebase's own words, worse: the order *did* land, is not adopted, gets no stop, and is journaled as a clean skip — "a live, STOPLESS 4-leg condor resting while the bot's journal says nothing happened." I asserted from the spec plus a summary instead of reading the code. That is precisely the failure mode v1.93's re-read rule was ratified to prevent, and I was the adviser it was written about. Recording it here rather than quietly fixing it.

---

## 1. Verdict on the agent's audit

The **shape** diagnosis is correct and valuable: all three primitives are built on `get_live_orders()` and every predicate derived from them answers a narrower question than its name implies. That is NFR-09, twice more, and the agent was right to pause before building on it.

Six specific claims hold. Two do not. Five significant things are missing, one of which I think is the most dangerous item in the whole area.

### 1.1 Confirmed

| Claim | Evidence |
|---|---|
| `cursor` accepted and entirely ignored | `fills_since(self, cursor)` never references `cursor`. Confirmed. |
| Returns orders, not fills; sim/live shape divergence | Live returns `PlacedOrder` objects; sim returns `{"order_id","price"}` dicts. Port declares `list[Fill]` where `Fill = Any`, so the type system permits it. |
| The window is the live-orders view | All three primitives call `get_live_orders()`. |
| `positions()` is an unfiltered passthrough | `return await self._account.get_positions(self._session)` — no filtering, no shape validation. Zero-quantity rows pass. |
| `SimulatedBroker.positions()` returns `[]` | Confirmed, line 295. **`git stash list` is empty on this clone** — whatever fix exists is on another machine and is not in the record. |
| `find_matching_order` returns `None` for an aged-out order | Confirmed — and see §1.3 N3 for the precise boundary. |

### 1.2 Refuted

**`.endswith("filled")` does NOT match "Partially Filled" — that status does not exist.** I read the pinned SDK. `tastytrade==13.0.0` `OrderStatus` (order.py:66) has exactly thirteen members:

```
Received, Cancelled, Filled, Expired, Live, Rejected, Contingent,
Routed, In Flight, Cancel Requested, Replace Requested, Removed,
Partially Removed
```

Only `Filled` ends in "filled". `Partially Removed` normalises to `"partially removed"`, which does not. **The predicate is correct as written.** As a bonus this also verifies the freshly-shipped `_KNOWN_ORDER_STATUS_TOKENS` table: it matches the SDK enum **thirteen for thirteen**, so v1.92's ratified vocabulary is complete and the STP-04a deny-list rests on solid ground.

**"Callers believe they're asking what filled since X" — no caller asks that.** All fourteen call sites pass `fills_since(None)` explicitly. The ignored cursor is a **latent** trap (the first caller to pass a real value gets silently wrong answers), not an active defect. That distinction changes the fix — see N7.

**The sim/live divergence is not "sitting in plain sight" unnoticed.** Every one of the nine consuming modules imports a single shared normaliser, `execute_entry._fill_matches`, with comments naming exactly this hazard ("a raw `.get(...)` here would crash on a live SDK fill") and attributing it to the 2026-07-11 reprice-race sweep. The divergence is real at the type level and should still be closed, but it is centrally mitigated and was found before, not now.

### 1.3 Missing — five findings, ranked

**N1 — `HOLDS_POSITION` must be DIRECTION-AWARE, or ORD-12 authorises the exact Buy-to-Open it exists to prevent.** *(Most important item here.)*

`CurrentPosition` (account.py:180) carries `quantity: Decimal` **as a magnitude**, with the side in a separate field, `quantity_direction: str`. A resolver matching on symbol + non-zero quantity cannot distinguish a **long** holding from a **short** one.

Every condor leg has a position. If the resolver says HOLDS_POSITION on symbol match alone and the close is a `buy_to_close` against a leg the account is **long**, the order *adds to the long* — re-establishing exactly the "close filled as Buy to Open" incident of 2026-07-20 that ORD-12 was written for. Symbol match is necessary and **not sufficient**: the predicate must assert the holding is on the side the close would **reduce**.

This is free — the field is already in the payload. `quantity_direction == "Zero"` also gives the clean filter for the zero-quantity rows the agent flagged.

**N2 — the partial-fill hole is real, but the opposite way round.** Because there is no `Partially Filled` status, a partially filled order sits at status **`Live`** with `Leg.remaining_quantity > 0` and `Leg.fills` populated. `fills_since` filters on status only, so **a partial is excluded entirely and reads as "no fill."** Real contracts are filled and every predicate — `_filled`, the stop-fill watch, the decay watcher, reconcile, EOD sweep, manual close — says nothing happened. Same absence-as-proven-negative shape, and it is ENT-11(6)'s ratified clause ("a cancelled-after-partial order must still report its filled legs, never nothing") currently unimplemented.

**N3 — the window is a DAY, and naming it precisely shrinks the problem.** `Account.get_live_orders` is `GET /accounts/{n}/orders/live`, documented as *"Get orders placed **today** for the account."* `PlacedOrder` is *"information about a live order, **whether it's been filled or not**."*

So: **intraday, nothing ages out and all three primitives are sound.** The blast radius is the **day boundary** — overnight crash recovery, restart-the-next-morning, the EOD sweep after rollover, and any question about yesterday. That is a bounded, named, testable surface rather than a vague "transient window," and it is where REC's crash-recovery SLA lives.

**N4 — the durable record already exists in this codebase.** `day_fills(day)` calls `Account.get_history(type="Trade")` — the broker's own transaction history, already used by RPT-15. The fix is therefore not "make `fills_since` durable." It is: **when the today-window says absent, escalate to the durable source before treating absence as proof.** That is NFR-09's UNKNOWN branch, implemented with code that already ships. Keep the live view as the fast path (it must stay cheap — it is polled in health loops); escalate only on a negative, which is the rare case.

**N5 — three position flags are silently dropped, and the probe is narrower than described.** `CurrentPosition` also carries `is_suppressed`, `is_frozen`, and `restricted_quantity`. A HOLDS_POSITION verdict on a frozen or restricted position authorises a close the broker will refuse. Separately: `get_positions()` accepts `symbol` and `instrument_type` filters, so the resolver can ask per-symbol server-side rather than pulling the book and matching client-side.

And the symbology question is more precisely scoped than "verify positions." `_leg_right` cites a **prod probe of 2026-07-21** that already observed futures **order-leg** symbols (`./ESU6 E3BN6 260721P7185`), and `fut_symbol.py` parses them. So the **order** side has recorded observations including /ES. The **position** side has none, for any underlying. The open question is exactly: *does `CurrentPosition.symbol` equal `Leg.symbol` byte-for-byte, per `instrument_type`?* Both models carry `instrument_type`, so the resolver can reuse `_leg_right`'s existing equity-vs-futures dispatch instead of inventing one.

**N6 (minor) — `fill_legs` reports ORDERED quantity.** `qty=int(Decimal(str(getattr(leg, "quantity", 0))))` — ENT-11(6) requires filled, never ordered. Two clean routes exist in the payload: `leg.quantity − leg.remaining_quantity`, or `sum(f.quantity for f in leg.fills)`.

**N7 (minor) — retire the cursor, don't implement it.** No caller wants it, the durable question is served by `day_fills`, and an unhonourable parameter is a can-never-say-no defect in miniature. Precedent is settled twice: STP-03 `stop_limit` tombstoned ("retire, not build"), DAT-04a halt input "retired, never stubbed."

### 1.4 One point of sequencing fact

`ORD-12` has **zero references in `backend/`**, and `TERMINAL_NO_POSITION` / `HOLDS_POSITION` appear only in `tests/features/TC-ENT-11.feature`. The resolver is specced and unbuilt, exactly as v1.92 sequenced it. Nothing can fail on the first live trade from a resolver that does not exist — the risk is **prospective**, which is the right time to be having this conversation and is to the agent's credit for pausing here.

---

## 2. Verdict on the probe

**Authorise it.** Three independent reasons:

1. **ENT-11(7) already mandates it.** "Parity must be OBSERVATION-based, not stub-vs-stub: each fake is checked against RECORDED broker observations." `positions()` has no recorded observation, so its mandatory parity test cannot exist in a spec-compliant form. Declining leaves a ratified rule unimplementable.
2. **It is what licenses `TERMINAL_NO_POSITION` at all.** Under ENT-11(3), evidence is positive and absence is never proof of absence. A negative verdict is only legitimate if you have verified you would recognise a matching row *if one existed*. Until that observation exists, a symbol comparison returning nothing is an assumption wearing an evidence badge.
3. **It is a read-only GET with no order-action capability**, on the same surface `day_fills` already uses in production.

**Widen it — same authorisation, five answers instead of one.** Markets are shut until Monday 2026-07-27, so there is time to make one session comprehensive:

1. Full `get_positions()` payload, every field, for every open position — **including `quantity_direction` values actually observed** (does "Zero" appear?).
2. `CurrentPosition.symbol` vs `Leg.symbol` byte-equality, bucketed by `instrument_type`.
3. Full `get_live_orders()` payload including `legs[].fills`, `remaining_quantity`, and observed status strings.
4. `external_identifier` round-trip fidelity on a bot-placed order — length, truncation, byte-equality.
5. A partially-filled order if one can be observed, to pin the `Live` + `remaining_quantity` representation in N2.

**Two constraints on the capture.** `CurrentPosition` includes `account_number`; strip it before anything is committed as a fixture. And the probe can only pin symbology for instruments actually held — if the account holds SPX/SPXW only, it proves SPX and proves nothing for RUT or /ES. That is fine provided the resolver treats an unobserved underlying as UNKNOWN rather than flat, which is §4's proposed ENT-11(3c).

---

## 3. Recommended sequence

1. **`SimulatedBroker.positions()` out of the stash and onto a branch, first.** Un-versioned work outside the record is a smaller version of the hazard that made v1.92 choose clean rebuild over surgery. And it blocks everything downstream: `return []` means paper refuses every exit under ORD-12, so the simulator cannot falsify the resolver you are about to build on it.
2. **Monday's read-only probe**, scoped as §2. Redact, commit as pinned vectors.
3. **One ordered-vs-filled correction** covering `fill_legs` qty (N6) and `fills_since` partial exclusion (N2) together — they are one defect in two places, and fixing either alone leaves the hole open. Retire the cursor in the same change (N7).
4. **Absence-escalation** (N4): today-window negative → durable `day_fills` check → only then ABSENT; otherwise UNKNOWN.
5. **Then** build the per-leg resolver, direction-aware from the first line (N1) rather than retrofitted.

One structural note on 3–5: rather than three separate patches, the three primitives want **one shared three-valued result** — `FOUND` / `PROVEN_ABSENT` / `UNKNOWN` — with UNKNOWN structurally impossible to collapse. NFR-09 already demands an explicit UNKNOWN branch; building the resolver against two-valued primitives and retrofitting UNKNOWN later is the expensive ordering.

---

## 4. Proposed amendment text (v1.94) — for ratification, not applied

> **NFR-09(a) — a today-window predicate names its own scope.** `get_live_orders()` returns *orders placed today*. Every predicate built on it answers "…today, at the broker", never "ever", and must say so in its own name or docstring. No caller whose question spans a day boundary — REC crash recovery, restart, EOD after rollover, any question about a prior day — may treat its negative as proof.
>
> **NFR-09(b) — absence escalates before it proves.** A negative from a today-window predicate resolves `UNKNOWN`, never `ABSENT`, until confirmed against the durable source (`day_fills` / `Account.get_history`). Only the durable source may license a "did not happen" conclusion. The live view remains the fast path; escalation occurs only on a negative.
>
> **NFR-09(c) — the `fills_since` cursor is RETIRED, never stubbed.** It was accepted and never honoured; all fourteen callers passed `None`. The parameter is removed and its absence is tested, per the STP-03 and DAT-04a retire-don't-build precedent. The durable question it appeared to answer is served by `day_fills`.
>
> **ENT-11(3a) — `HOLDS_POSITION` is DIRECTION-AWARE.** `CurrentPosition.quantity` is a magnitude; the side is `quantity_direction`. `HOLDS_POSITION` requires a symbol match **and** that the holding lies on the side the close would REDUCE. A symbol-only match would authorise a `buy_to_close` against a LONG holding — the exact Buy-to-Open of the 2026-07-20 incident. `quantity_direction == "Zero"` is never a holding. `is_frozen`, `is_suppressed`, and `restricted_quantity` are carried through the predicate, never dropped.
>
> **ENT-11(3b) — recognition precedes negation.** `TERMINAL_NO_POSITION` may be returned only for an underlying and `instrument_type` whose `CurrentPosition.symbol` format has a RECORDED observation. Absent that observation, the resolver returns `UNKNOWN`. Rationale: a negative from an unvalidated symbol comparison is an assumption, not evidence (ENT-11(3): absence of a record is never proof of absence of a position).
>
> **ENT-11(6a) — ordered-vs-filled, both sites.** `fill_legs` reports FILLED quantity (`leg.quantity − leg.remaining_quantity`, or Σ`leg.fills[].quantity`), never ordered. `fills_since` includes partially-filled orders, which the broker carries at status `Live` with non-empty `leg.fills` — a status-only filter is structurally incapable of seeing them. **Pinned fact:** `tastytrade==13.0.0` `OrderStatus` has thirteen members and no `Partially Filled`; partial state lives in `remaining_quantity` and `fills`.
>
> **ENT-11(7a) — the positions contract observation.** A read-only market-hours capture of `get_positions()` and `get_live_orders()` is recorded as pinned vectors before the resolver is built, with `account_number` redacted. Parity fixtures for `positions()` derive from it. An underlying with no such observation is governed by ENT-11(3b).

---

## 5. Message to the coding agent

*(reproduced separately for copy-paste)*
