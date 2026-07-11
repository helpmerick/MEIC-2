# 04 — Test Cases

Gherkin acceptance tests. Every rule in doc 01 and every edge case in doc 02 is covered; the traceability matrix at the end is the completeness contract — **CI must fail if a rule ID has no passing test**.

Test harness requirements (see doc 05 for ports): all tests run against a **FakeBroker** and **FakeMarketData** implementing the same ports as the tastytrade adapters, with a controllable **FakeClock**. No test may hit the real API. Fakes must support: scripted fills/partials/rejects/timeouts, injected latencies, stream staleness, and crash/restart simulation (new bot instance against the same fake broker state + persisted event log).

Standard fixture unless stated otherwise:
```
Given config defaults from doc 06
And today is a full trading day
And entry 1 fills a condor as one 4-leg order, allocated leg fills:
    short put 5990 @ 1.35, long put 5940 @ 0.15   (put side net credit 1.20)
    short call 6060 @ 1.25, long call 6110 @ 0.15 (call side net credit 1.10)
And total condor credit = 2.30, stop_loss_pct = 95%
And stop_basis = short_premium => put trigger = floor_to_tick(1.35 * 1.95) = 2.60,
    call trigger = floor_to_tick(1.25 * 1.95) = 2.40
And stop_basis = total_credit => both triggers = floor_to_tick(0.95 * 2.30) = 2.15
And stop_basis = per_side => put trigger = floor_to_tick(1.35 + 0.95*1.20) = 2.45,
    call trigger = floor_to_tick(1.25 + 0.95*1.10) = 2.25
```

---

## Entry & scheduling

**TC-ENT-01** — ENT-01/ENT-02
```gherkin
Scenario: Entry executes inside its window
  Given the clock reaches 10:00:00 ET
  When the entry attempt begins within entry_window_seconds
  Then a 4-leg condor limit order is submitted per ORD-01/ORD-02

Scenario: Missed window is never executed late
  Given the bot was down from 09:55 to 10:05 ET
  When the bot restarts at 10:05
  Then entry 1 is marked SKIPPED with reason "missed_window"
  And no order for entry 1 is ever submitted
```

**TC-ENT-02** — ENT-03 (one scenario per gate: Stop Trading active, Flatten All executing, halt, stale data, invalid session, insufficient BP — parametrized)
```gherkin
Scenario Outline: Pre-entry gate blocks entry
  Given <gate_condition> is true at 10:30 ET
  Then entry 2 is SKIPPED with reason <reason>
  And no order is submitted

  Examples:
    | gate_condition            | reason              |
    | Stop Trading active       | stop_trading        |
    | a Flatten All executing   | flatten_in_progress |
    | a market halt             | market_halted       |
    | market data stale         | data_unavailable    |
    | broker session invalid    | invalid_session     |
    | insufficient buying power | insufficient_bp     |
```

**TC-ENT-03** — ENT-04/ENT-05: each fill's quantity equals its OWN row's `contracts` (v1.44: schedule rows 2 and 1 ⇒ fills of 2 and 1); values outside 1–10 rejected; RSK-04 evaluates Σ(per-entry worst case) — 2 × wc₁ + 1 × wc₂, never 3 × max(wc); a day never exceeds max_entries_per_day fills.

**TC-ENT-08** — ENT-09/UI-22 manual entry
```gherkin
Scenario: Manual fire passes every gate except the window
  Given the operator presses the manual fire button at 10:07, outside any scheduled window
  And all ENT-03 gates pass
  When the OK-confirmation dialog is acknowledged
  Then exactly one entry attempt runs through the identical pipeline
  And the entry is recorded with initiator "manual_entry"
  And it counts toward max_entries_per_day

Scenario: No fire without the OK dialog
  Given the operator presses the manual fire button
  When the dialog is dismissed or times out
  Then no order is submitted and no attempt is recorded

Scenario: Gates are never bypassed
  Given Stop Trading is ON
  When the operator presses the manual fire button and acknowledges OK
  Then the attempt is refused with skip reason "blocked" shown on the card

Scenario: RSK-04 vetoes a manual entry like any other
  Given open entries whose summed worst case leaves less headroom than the manual entry needs
  When the manual entry is confirmed
  Then it is skipped with reason "max_day_risk"

Scenario: Double-click is one attempt
  When the operator presses the button twice and confirms once
  Then exactly one order exists (idempotency key per press-confirmation)
```

**TC-ENT-09** — ENT-09b manual minimum short-strike floors (v1.57)
```gherkin
Scenario: A floor filters the walk without changing it
  Given SPX at 7480 and a manual fire with put floor 7450
  And the probe walk would normally match the 7460 put
  Then strikes inside the floor are excluded and the walk selects at or beyond 7450
  And the call side runs default behaviour when no call floor is set

Scenario: Credit rules are never weakened by a floor
  Given floors that leave no strike satisfying 1.00 gross and 2.00 total net
  Then the fire skips with reason "no_valid_strikes" and no order is placed

Scenario: Refuse and re-pick when spot crosses a floor
  Given the dialog opened with SPX 7480 and call floor 7500 selected
  When SPX is 7505 at OK time
  Then the fire is REFUSED with reason "floor_inside_spot"
  And the operator must re-select before any order can be placed

Scenario: Dropdowns come from the validated universe only
  Then every selectable strike has fresh two-sided quotes at dialog population
  And each row shows strike, points from spot, and live mid

Scenario: Floors are evented for audit
  Given a manual fire with put floor 7450 and no call floor
  Then the entry events record the floors and the day report shows them
```

**TC-ENT-10** — ENT-10/UI-24 arm runs the day (v1.53)
```gherkin
Scenario: Arming starts the watcher and the entry fires at its time
  Given a composed schedule with one future entry
  When the operator arms successfully
  Then the day task is watching and the entry fires at its time through the full gate chain

Scenario: Boot restore resumes the watcher
  Given persisted state is ARMED with entries remaining
  When the bot boots
  Then the day task starts automatically without operator action

Scenario: Disarm stops future entries atomically
  Given an entry attempt is in flight when the operator disarms
  Then the attempt completes or cancels cleanly and is never abandoned mid-flight
  And no further entries fire

Scenario: Mid-day edits can never renumber or drop an entry (durable ids)
  Given rows A(fired), B(pending 11:15), C(pending 12:35) with durable ids
  When the operator deletes fired row A while ARMED
  Then rows B and C keep their ids, B fires at 11:15, and nothing is skipped or double-fired

Scenario: A crashed day task alerts and stays down
  Given the day task dies with an error while ARMED
  Then a critical alert is raised and the task is NOT auto-restarted until Disarm then Arm
```

**TC-DAY-06** — DAY-06 entry-time format & window (v1.53)
```gherkin
Scenario Outline: Non-military formats are rejected per row
  When a schedule row's time is "<bad>"
  Then validation rejects it with reason "not_24h_military"
  Examples:
    | bad    |
    | 1:53pm |
    | 0930   |
    | 24:00  |
    | 11:60  |
    | 11-53  |

Scenario: Valid formats pass and dots canonicalise
  Then 09:32, 9:32, 15:30 and 23:59 pass the format gate
  And 11.53 persists as 11:53 and 9.32 persists as 09:32

Scenario: The RTH window is enforced on the value
  Then 08:00 and 16:30 are rejected with reason "outside_market_hours"
  And 09:30 (the open edge) saves
  And the format and window checks are backend-authoritative
```

**TC-UI-05** — UI-23 local-time echo (v1.53)
```gherkin
Scenario: ET times echo in the operator's local zone
  Given the operator's browser zone is Europe/London
  When a row's ET time is 11:53
  Then "16:53 London" (approx) renders beneath the cell
  And DST is tracked automatically per instant
  And an invalid time shows the precise rejection reason instead of an echo
```

**TC-UI-06** — UI-24 next-entry countdown (v1.53)
```gherkin
Scenario: The countdown proves the schedule is being watched
  Given the bot is ARMED with a next entry composed
  Then the panel shows the entry's ET time and a ticking countdown
  And the value derives from the backend's seconds_to_next, never the browser clock
  And DISARMED shows "schedule idle" and an exhausted schedule shows "no more entries today"
```

**TC-ENT-04** — ENT-06/EC-ENT-10: VIX above vix_max ⇒ skip, info-level only; blackout date ⇒ skip.

**TC-ENT-05** — ENT-07/EC-ENT-11
```gherkin
Scenario: Working entry order cancelled before next entry
  Given entry 2's order is still WORKING at 11:00 ET
  When entry 3's scheduled time arrives
  Then entry 2's order is cancelled and cancellation confirmed
  And any partial fill is resolved per EC-ENT-06 before entry 3 begins
```

**TC-ENT-07** — ENT-01/01a arm/disarm
```gherkin
Scenario: Disarmed means nothing fires, ever
  Given 3 entries are composed in the UI but the operator never pressed Arm
  Then no entry attempt occurs at any scheduled time
  And existing positions remain fully managed (stops, LEX, TPF)

Scenario: Arming an empty schedule is rejected
  Given zero entries are composed
  Then the Arm action fails validation with an explanatory error

Scenario: The operator's count is the count
  Given the operator composed exactly 4 entries and armed
  Then exactly 4 entry attempts run, at exactly the composed times

Scenario: Disarm mid-day stops future entries only
  Given 4 entries armed, 2 already filled
  When the operator disarms at 11:45
  Then the remaining 2 entries never fire
  And the 2 open condors keep their stops and full management

Scenario: Armed state persists across days (standing schedule)
  Given the operator armed 6 entries on Monday
  When Tuesday's market opens with no operator action
  Then the day self-initializes (calendar, reconcile, warm-up)
  And all 6 entries fire at their times on Tuesday, and every trading day after, until the operator disarms

Scenario: Disarmed state equally persists
  Given the operator disarmed on Monday afternoon
  Then no entries fire on Tuesday, Wednesday, or any day until re-armed

Scenario: Docker/process restart restores the armed state
  Given the system was ARMED with 6 entries and the container dies at 10:47
  When the container recovers at 10:52
  Then the bot boots ARMED (state restored from the durable store)
  And the 10:30 entry (window missed while down) is SKIPPED missed_window
  And the 11:00 and later entries fire normally
  And a restart while DISARMED boots DISARMED

Scenario: Confirm Live is the third required state (ENT-01b)
  Given the system is ARMED with Stop Trading off
  But Confirm Live is OFF
  Then no entry fires at any scheduled time
  And the dashboard states which gate is blocking

Scenario: The full persistent-state inventory survives Docker recovery (REC-07)
  Given ARMED = on, Stop Trading = on, Confirm Live = on, trading_mode = paper, a standing 6-entry schedule, an armed TPF floor, and a paper cash ledger
  When the container dies and recovers
  Then every item is restored exactly as it was
  And entries remain blocked (Stop Trading is on) until the operator resumes
  And the paper ledger balance is unchanged

Scenario: Fresh install defaults safe
  Given a first-ever boot with no persisted state
  Then DISARMED, Stop Trading off, Confirm Live OFF
```

**TC-ENT-06** — ENT-08 pre-entry warm-up
```gherkin
Scenario: Near-expiry token renewed at warm-up, entry fires on time
  Given the session token expires in 200 seconds at T-60
  When the warm-up probe runs
  Then the token is renewed before T-30
  And the 10:30 entry begins exactly on schedule with fresh quotes

Scenario: Dropped stream resubscribed at warm-up
  Given the DXLink chain subscription is silently stale at T-60
  Then the warm-up resubscribes and quotes are fresh (STK-04) at fire time

Scenario: Warm-up cannot restore the session
  Given token renewal fails repeatedly from T-60
  Then an alert is raised at T-10
  And at fire time the entry is SKIPPED with reason "invalid_session"
  And the entry time itself was never delayed

Scenario: Bot starts inside the warm-up window
  Given the bot finishes recovery at T-30
  Then the warm-up probe runs immediately (compressed), not skipped
```

## Strike selection & credit gates

**TC-STK-01** — STK-02 delta method: with a scripted chain, the selected short strikes are the closest to 0.10Δ not exceeding 0.15Δ; boundary case at exactly short_delta_max selects the strike.

**TC-STK-02** — STK-02/02a premium principles (selection mechanics themselves live in TC-STK-08)
```gherkin
Scenario: target_premium reads the SHORT leg only
  Given the probe walk matches a short strike
  Then the long wing is placed at wing_width regardless of its own cost  # STK-03

Scenario: Expensive wing aborts on the total NET floor
  Given both shorts match their probes and wings cost 2.10 each (total net = 1.90)
  Then the entry is SKIPPED with reason "insufficient_credit"  # STK-06: total NET < 2.00 aborts

Scenario: A thin side trades when the total floor passes (accepted by design)
  Given the put side nets 0.10 after an expensive wing and the call side nets 2.20
  And both shorts collected >= min_short_premium
  Then the entry proceeds (total net 2.30 >= 2.00)   # per-side NET floor deliberately does not exist

Scenario: Stops and P&L use net fill credit, never target_premium
  Given the condor fills with short put 3.00 and long put 1.00
  Then stop math uses the actual net credit (not 3.00)
  And the day report shows short premium and net credit as separate labelled figures  # UI-14
```
Also: STK-07 missing wing strike ⇒ skip `no_valid_strikes` (EC-ENT-02).

**TC-STK-03** — STK-05/STK-06/EC-ENT-03/04
```gherkin
Scenario: Put side credit below minimum skips the whole entry
  Given the short put's mid = 0.80 and min_short_premium = 1.00
  Then the entry is SKIPPED with reason "insufficient_credit"
  And no order of any kind is submitted   # single-side entries prohibited
```

**TC-STK-06** — STK-09 strike collision (Ash's rules)
```gherkin
Scenario: Long at the desired short strike forces one shift
  Given entry 3's target short put strike 5990 holds an existing long
  Then the short shifts to 5985 and the wing moves with it (width preserved)

Scenario: Three blocked strikes abort the entry
  Given existing longs at 5990, 5985 and 5980 (the original and both shift targets)
  Then the entry is SKIPPED with reason "strike_collision" and no order is submitted

Scenario: Same type stacks - shorts on shorts
  Given entry 1 is short 5990 and entry 3's selection also lands on 5990
  Then no shift occurs and the order is submitted
  And both entries' fills and stops attribute correctly by order ID

Scenario: Same type stacks - longs on longs
  Given the wing target already holds another entry's long
  Then no shift occurs

Scenario: Long shifts alone when its target holds a short (width widens)
  Given the short places at its original strike
  But the wing target 5940 holds an existing short position
  Then the long shifts alone to 5935 (spread now 5 points wider)
  And RSK-04 evaluates the widened worst case before submission
  And five failed long shifts abort the entry with "strike_collision"

Scenario: In-flight opposite-type orders count as occupied
  Given an unfilled working order includes a long at 5990
  When the next entry wants a short at 5990
  Then 5990 is treated as blocked
  But an unfilled SHORT at 5990 does not block a new short there  # same type never blocks

Scenario: Gates re-run on final strikes
  Given the shifted short's premium falls below min_short_premium (or total net < min_total_credit)
  Then the entry is SKIPPED with reason "insufficient_credit"
```

**TC-STK-08** — STK-02 probe walk, operator-ratified vectors (v1.39; all mids shown are raw, target 3.00 unless stated)
```gherkin
Scenario: Vector A - first down-probe matches
  Given strikes with raw mids 3.20, 2.93, 2.70   # 2.93 rounds to probe price 2.95
  Then probes run 3.00 (miss), 2.95 (MATCH)
  And the 2.93 strike is sold

Scenario: Vector B - up-probe within the cap matches
  Given strikes with raw mids 3.30, 3.05, 2.80
  Then probes run 3.00, 2.95, 3.05 (MATCH)
  And the 3.05 strike is sold

Scenario: Vector C - equal distance above the cap is NEVER selected
  Given strikes with raw mids 3.45, 3.20, 2.80
  Then all seven probes 3.00 to 3.15 miss
  And the down-only phase matches 2.80
  And the 3.20 strike is never selected despite equal distance to target

Scenario: Vector D - full exhaustion skips
  Given no strike's rounded mid lies between 1.75 and 3.15
  Then all 3 up-probes and all 25 down-probes miss
  And the entry is SKIPPED with reason "no_valid_strikes"

Scenario: Vector E - deep walk sells thin but legal premium
  Given a strike with raw mid 1.80 and nothing nearer the 3.00 target
  Then the down-only phase matches at probe 1.80 (within the 25-step depth)
  And the strike is sold   # 1.80 >= the 1.00 hard floor

Scenario: Vector E2 - the 1.00 hard floor beats the walk depth
  Given target 2.00 and the only match would be at raw mid 0.95
  Then the effective floor is max(2.00 - 1.25, 1.00) = 1.00
  And probes below 1.00 are never taken
  And the entry is SKIPPED with reason "no_valid_strikes"

Scenario: Rounding lattice is nearest-0.05
  Given a strike with raw mid 2.92
  Then it answers probe 2.90, not 2.95   # 2.92 rounds down to 2.90

Scenario: Probe order is deterministic and logged
  Then the exact sequence T, T-0.05, T+0.05, T-0.10, T+0.10, T-0.15, T+0.15, T-0.20, T-0.25 ... is enumerated verbatim
  And the day report records which probe number matched
```

**TC-STK-07** — STK-10/11 chain integrity (Bug #1 regression suite)
```gherkin
Scenario: Holey near-ATM chain blocks selection, heals, entry proceeds
  Given only 75% of strikes within the ATM band have marks at fire time
  Then no strike selection occurs and the gate retries every chain_retry_seconds
  When the chain completes at T+20s (within the entry window)
  Then selection proceeds normally

Scenario: Persistent holes skip the entry at window expiry
  Given the entry's trade-relative reachable strike set never reaches chain_completeness_pct within entry_window_seconds
  Then the entry is SKIPPED with reason "incomplete_chain" and no order is submitted

Scenario: Probe-match integrity invariant (STK-11, v1.39)
  Given the probe walk selects a strike
  Then its raw mid is within 0.025 of the matched probe price
  And the day report records the matched probe number

Scenario: Missing wing retries within the window
  Given the wing strike has no mark at fire time but appears at T+15s
  Then the entry proceeds with the correct wing (no guessing, no immediate skip)

Scenario: Far-OTM emptiness never trips the gate
  Given strikes outside the ATM band have no bids
  Then the chain-integrity gate still passes

Scenario: Far-OTM dead strikes never block (v1.51 regression, live 2026-07-09)
  Given every strike in the entry's reachable set has fresh two-sided marks
  And calls 55+ points OTM outside the reachable set are listed but never quoted
  Then the STK-10 gate PASSES and selection proceeds

Scenario: A dead long wing is caught upfront
  Given the reachable set includes the wing strike and its quote is missing
  Then the gate counts it against completeness (no later wing_unmarked surprise)

Scenario: chain_atm_band_pts is retired
  Given config contains chain_atm_band_pts
  Then config validation rejects it as an unknown retired key
```

**TC-STK-04** — STK-04/DAT-02: greeks older than max_quote_age_ms ⇒ entry aborted.

**TC-STK-05** — STK-08: all order and trigger prices land on valid ticks for both the sub-$3 and $3+ regimes.

## Entry order execution

**TC-ORD-01** — ORD-02/ORD-03
```gherkin
Scenario: Reprice ladder walks down and respects the floor
  Given the entry order does not fill
  When entry_reprice_seconds elapses 5 times
  Then the limit was repriced down one tick each time
  And never below min_total_credit
  And after the final attempt the order is cancelled and entry SKIPPED "unfilled_at_floor"
```

**TC-ORD-02** — ORD-04/EC-API-03
```gherkin
Scenario: Submit timeout does not cause duplicate orders
  Given the broker accepts the order but the submit response times out
  When the bot queries by idempotency key
  Then it discovers the existing order and does NOT resubmit
```

**TC-ORD-03** — ORD-05/EC-ENT-12: cancel/fill race — fill event after cancel sent ⇒ condor treated as OPEN, stops placed.

**TC-ORD-04** — EC-ENT-06 partial fill: (a) balanced partial — 1 of 2 condors filled at cancel ⇒ filled condor kept and protected (stops placed), quantity recorded; (b) unbalanced-leg anomaly injected via fake broker ⇒ completion attempted for partial_fix_seconds, else filled legs flattened; no unbalanced position remains; critical alert fired.

**TC-ORD-05** — EC-ENT-07/08: BP rejection skips with lockout after 2 consecutive; other rejection retried once then skipped.

**TC-ORD-07** — ORD-09 broker-truth leg identity (v1.45)
```gherkin
Scenario: Fill events record broker-reported symbols and allocations
  Given a condor fill is confirmed by the broker
  Then the fill event records, for each of the 4 legs, the broker-reported OCC symbol and allocated price
  And the recorded symbols are byte-identical to the broker payload

Scenario: Every later order action uses the recorded symbol
  Given a recorded fill with leg symbols
  When a stop, LEX sell, decay buyback, close, or flatten order is built for a leg
  Then the order's instrument symbol is the recorded one
  And no code path reconstructs the symbol from strike and expiry at action time

Scenario: Reconstruction only ever cross-checks
  Given a recorded symbol that disagrees with reconstruction from the condor's strikes
  Then an alert is raised naming both values
  And the recorded symbol is still the one used

Scenario: Paper records simulator symbols identically
  Given a paper-mode fill
  Then the fill event carries simulator-assigned leg symbols in the same fields
```

**TC-ORD-08** — ORD-09/STP-02: recorded fill credit is the BROKER'S, never the order's (live incident 2026-07-09, order 482390058: limit 3.50, broker-allocated net 3.60)
```gherkin
Scenario: Net credit comes from the broker's fill, not the working limit
  Given a 4-leg entry limit working at net credit 3.50
  And the broker reports per-leg fill allocations: shorts 1.80 and 1.95, longs 0.08 and 0.07
  When the fill is recorded
  Then the entry's net credit is 3.60 (sum of allocated legs)
  And never the 3.50 working limit or any pre-fill estimate

Scenario: Missing allocations are never fabricated
  Given the broker reports the fill without a usable per-leg allocation
  When the fill is recorded
  Then the order-level fill price is used for net credit
  And no per-leg price is ever fabricated (ORD-09; the STP-02d reconciliation record logs FAIL)
```

**TC-STP-19** — STP-02: stops are computed from the ACTUAL credit received (live incident 2026-07-09)
```gherkin
Scenario: Trigger uses the actual net fill credit
  Given an entry filled at actual net credit 3.60 with stop_basis total_credit at 95 percent
  When protective stops are placed
  Then each trigger = floor_to_tick(0.95 * 3.60) = 3.40
  And never 95 percent of the 3.50 working limit or the pre-fill mid estimate
  And this agrees with TC-STP-16 vector 3 (3.42 floors to 3.40)
```

## Stops

**TC-STK-09** — STK-10 v1.55 baseline pre-validation
```gherkin
Scenario: Dead-at-baseline strikes never count as holes
  Given warm-up validates 24 of 28 reachable strikes (4 far wings listed but never quoted)
  And at fire time 23 of the 24 validated strikes are still fresh
  Then completeness = 95.8 percent and the gate PASSES
  And under the pre-v1.55 rule the same day would have falsely skipped at 85.7 percent

Scenario: A genuine feed regression still fails
  Given warm-up validated 24 strikes and only 12 remain fresh at fire time
  Then the gate fails and the entry retries then skips incomplete_chain

Scenario: A sliver baseline cannot trivially pass
  Given warm-up finds only 5 validated strikes on the call side with min_validated_strikes = 10
  Then a warm-up alert fires 60 seconds before the window and the entry retries
  And an unhealed baseline skips incomplete_chain

Scenario: A dead wing is a candidate skip, not an entry failure
  Given a candidate short whose wing strike is not in the validated universe
  Then that candidate is skipped and the probe walk continues
  And the entry fails only if no valid candidate remains

Scenario: Manual entries baseline at press
  Given the operator fires manually with no warm-up
  Then the validated universe is captured at press time under the same rules
```

**TC-DAY-07** — DAY-01a/ENT-10(7)/UI-24a exchange calendar (v1.60, from the Saturday-countdown incident)
```gherkin
Scenario: Holiday observance quirks compute correctly
  Then New Year's Day falling on Saturday is NOT observed (real vector: 2021-12-31 was a full trading day)
  And Saturday holidays observe Friday, Sunday holidays observe Monday
  And Good Friday derives from the Easter computus for any year
  And July 3 (Mon-Thu), the day after Thanksgiving, and Christmas Eve (Mon-Thu) are 13:00 ET half-days
  And the computed calendar matches published NYSE calendars pinned as vectors

Scenario: No day task exists on a closed day
  Given the bot is ARMED on a Saturday
  Then the supervisor starts no day task and zero EntrySkipped events enter the journal
  And the ENT-03 fire-time market-open gate remains in force unchanged

Scenario: The countdown never promises a closed-day entry
  Given a Saturday with the next trading day Monday and first entry 11:56 ET
  Then the panel shows "Mon 11:56 ET" with a day-spanning countdown
  And "no more entries today" appears only for an exhausted schedule on a trading day

Scenario: An empty calendar is a construction error
  Given live gates constructed with no holiday data
  Then boot fails loudly rather than treating holidays as open days

Scenario: The local echo is DST-correct across the switch
  Given a next entry lying on the far side of a DST transition
  Then the local echo converts the full instant, not today's offset
```

**TC-STP-01** — STP-01/STP-02
```gherkin
Scenario: Stops placed immediately on fill (total_credit basis - THE DEFAULT, Ash's outcome contract)
  Given stop_basis = total_credit
  When the condor fill is confirmed
  Then two buy-to-close stop-market orders (TIF Day) are working within the same processing turn
  And each trigger price = floor_to_tick(0.95 * 2.30)   # -> 2.15, not 2.20
  And no stop exists on either long leg   # STP-06

Scenario: The outcome contract holds (Ash's Way 2, the 400-dollar example)
  Given net credit 4.00, both stops at 3.80, longs recover zero
  Then one side stopped and one side expiring nets +0.20 (small profit, the kept 5%)
  And both sides stopped nets -3.60 (about the premium, never more before slippage)

Scenario: Stops placed immediately on fill (short_premium basis, selectable per entry)
  Given stop_basis = short_premium
  Then the put stop trigger = floor_to_tick(1.35 * (1 + 0.95))   # -> 2.60
  And the call stop trigger = floor_to_tick(1.25 * (1 + 0.95))   # -> 2.40
  And neither trigger depends on any long leg's allocated fill price

Scenario: Stops placed immediately on fill (per_side basis)
  Given stop_basis = per_side
  When the condor fill is confirmed
  Then the put stop trigger = floor_to_tick(1.35 + 0.95 * 1.20)   # -> 2.45
  And the call stop trigger = floor_to_tick(1.25 + 0.95 * 1.10)   # -> 2.25
  And side net credits are computed from the broker's allocated leg fills
```

**TC-STP-02** — STP-02 parametrization: for each pct in {95, 100, ..., 300} × each stop_basis ∈ {short_premium, total_credit, per_side}, triggers match the formulas (per_side formulas stay verified in the domain even while gated); values outside the pct set or basis set are rejected by config validation (doc 06); SELECTING per_side is rejected `allocation_unverified` per STP-02d (v1.43).

**TC-STP-03** — STP-02 intraday change / EC-STP-07: pct changed 95→150 (and basis per_side→total_credit) after entry 1 ⇒ entry 1 stops unchanged, entry 2 uses the new values.

**TC-STP-04** — STP-04/EC-STP-01, STP-01 quantity invariant (v1.45)
```gherkin
Scenario: Unconfirmed stop escalates to UNPROTECTED handling
  Given the broker rejects stop placement stop_retry_attempts times
  Then the affected side is flattened per unprotected_action
  And a critical alert is raised
  And total unprotected time <= stop_retry_seconds * stop_retry_attempts

Scenario: Stop quantity must equal the short position it protects
  Given an entry filled with contracts = 2
  When a stop is confirmed working with quantity 1
  Then the mismatch is detected at placement confirmation
  And the condition is handled as UNPROTECTED per STP-04
  And a critical alert names the naked quantity

Scenario: Reconcile catches a quantity mismatch that arose later
  Given a working stop whose quantity no longer equals the short leg's ledger quantity
  When reconcile runs
  Then the entry is treated as UNPROTECTED (or OWN-10 if operator-resized)
  And the bot never silently resizes the stop itself
```

**TC-STP-05** — EC-STP-02 crash between fill and stop placement: restart ⇒ REC-04 places stops; assert idempotency (no duplicates if one stop had actually been accepted).

**TC-STP-06** — STP-07/STP-08/EC-STP-04 whipsaw: put side stops, then call side stops; both run LEX independently; both losses in day P&L.

**TC-STP-07** — EC-STP-03: scripted gap fills stop 8 ticks past trigger ⇒ slippage recorded, alert fired at threshold.

**TC-STP-08** — STP-05/UC-12 stop independence: with a working stop, simulate bot disconnect; on reconnect the stop order is still working at the (fake, then paper-integration) broker with unbroken timestamps.

**TC-STP-09** — STP-03/EC-STP-08: with stop_limit configured, a triggered-unfilled stop is cancelled/replaced with market after stop_limit_escalation_seconds.

**TC-STP-10** — EC-STP-05 partial stop fill: LEX starts only on full short close (default config).

**TC-STP-11** — EC-STP-06: stop filled while bot down ⇒ on restart the missed event is synthesized and LEX begins.

**TC-STP-12** — EC-STP-10: duplicate stops found on reconcile ⇒ surplus cancelled first.

**TC-STP-14** — STP-02b manual rebate markup
```gherkin
Scenario: Markup raises the trigger in per_side basis
  Given stop_basis = per_side, stop_loss_pct = 95, stop_rebate_markup = 0.50
  Then the put stop trigger = floor_to_tick(1.35 + 0.95*1.20 + 0.50)   # raw 2.99 -> 2.95, NOT 3.00 (round would cross the 0.10-tick regime)
  And the call stop trigger = floor_to_tick(1.25 + 0.95*1.10 + 0.50)   # raw 2.795 -> 2.75

Scenario: Markup raises the trigger in total_credit basis
  Given stop_basis = total_credit and the same markup
  Then both triggers = floor_to_tick(0.95*2.30 + 0.50)   # raw 2.685 -> 2.65

Scenario: Default markup of zero changes nothing
  Given stop_rebate_markup = 0.00
  Then triggers are byte-identical to the pre-markup formulas

Scenario: NLE and calibration incorporate the markup
  Given a markup of 0.50 in force
  Then the NLE estimate is computed from the markup-inclusive trigger
  And the calibration record for a stop event stores markup = 0.50

Scenario: UI worst-case disclosure
  Given the operator sets markup 0.50 in the UI
  Then the setting displays the worst-case increase before saving  # UI-18

Scenario: Intraday change is next-entry only
  Given markup changed 0.00 -> 0.50 after entry 1 filled
  Then entry 1's resting stops are unchanged and entry 2 uses 0.50
```

**TC-STP-16** — Operator-ratified stop-calculation vectors (v1.39; stop_basis total_credit, markup 0.00 and floor-to-tick rounding unless stated; all values exact to the cent)
```gherkin
Scenario: Vector 1 - the canonical 400-dollar contract
  Given shorts 3.00 + 2.00, wings 0.50 + 0.50, net credit 4.00, pct 95
  Then both triggers = 3.80 exactly
  And one side stopped with the other expiring nets +20
  And both sides stopped nets -360

Scenario: Vector 2 - pct 100 boundary
  Given the same trade at pct 100
  Then both triggers = 4.00, one-side nets 0, both-sides nets -400 exactly

Scenario: Vector 3 - floor rounding in the 0.10-tick regime
  Given net credit 3.60 at pct 95 (raw trigger 3.42)
  Then the trigger floors to 3.40, never 3.50

Scenario: Vector 4 - floor rounding in the 0.05-tick regime
  Given net credit 3.10 at pct 95 (raw trigger 2.945)
  Then the trigger floors to 2.90, never 2.95

Scenario: Vector 5 - markup spends the one-side guarantee (documented consequence)
  Given vector 1 plus stop_rebate_markup 0.50
  Then both triggers = 4.30
  And a one-side hit nets -30 plus long recovery   # the +20 guarantee is traded away by the dial
  And both sides nets -460

Scenario: Vector 6 - feasibility kill
  Given shorts 3.00 + 2.00 with wings 1.50 + 1.50 (net credit 2.00, raw trigger 1.90)
  Then the trigger sits below the 3.00 short and the entry is SKIPPED "infeasible_stop"

Scenario: Vector 7 - feasibility knife-edge
  Given net credit 3.37 at pct 95 (raw 3.2015, floors to 3.20) vs a 3.00 short
  Then clearance is exactly 2 ticks and the entry is FEASIBLE   # rule is >=

Scenario: Regression guard - the corrected behavior can never silently return
  Given vector 1 with stop_basis = total_credit
  Then the trigger MUST be 3.80 and MUST NOT be 5.85
  # failure message: "per-leg (short_premium) default has crept back in"
```

**TC-STP-15** — STP-02c feasibility guard (v1.38)
```gherkin
Scenario: Thin credit is skipped before entry
  Given estimated net credit 2.00 at 95% (trigger 1.90) and the short put mid is 3.00
  Then the entry is SKIPPED with reason "infeasible_stop" and no order is submitted

Scenario: Healthy credit passes
  Given estimated net credit 4.00 (trigger 3.80) vs shorts at 3.00 and 2.00
  Then triggers clear both fills by the minimum distance and stops are placed

Scenario: Post-fill infeasibility closes instead of placing a suicidal stop
  Given fills land such that the actual trigger does not clear a short's fill
  Then no stop is placed for that entry
  And the entry closes via CLS-01 with initiator "infeasible_stop" and an alert

Scenario: Markup counts toward feasibility
  Given a rebate markup that lifts the trigger above fill + minimum distance
  Then the entry is feasible   # STP-02b adds to the trigger before the check
```

**TC-STP-17** — STP-03b stop watchdog (v1.41)
```gherkin
Scenario: Silent in the normal world
  Given the mark crosses the trigger and the broker stop fills within 6 seconds
  Then the watchdog never alerts and never acts

Scenario: Alert at grace, escalate at bound
  Given the mark holds at or above trigger and the resting stop stays unfilled
  Then a critical alert fires at 10 seconds
  And at 20 seconds the bot sends a marketable buy-to-close and cancels the resting stop
  And the side proceeds SIDE_STOPPED into LEX with initiator watchdog_escalation

Scenario: Race - broker stop fills during escalation
  Given the resting stop fills while the escalation order is in flight
  Then the escalation aborts per ORD-08 and exactly one buy-back exists (order count = 1)

Scenario: Stale marks pause the clock
  Given quotes go stale mid-breach
  Then the watchdog clock pauses and resumes on fresh data; no action on stale marks

Scenario: Every escalation is calibration evidence
  Then each watchdog_escalation record stores mark-at-breach, elapsed time, and fill price
```

**TC-STP-18** — STP-02d per_side allocation gate (v1.43)
```gherkin
Scenario: per_side selection is rejected while the gate is in force
  Given config stop_basis = per_side, globally or on any entry override
  Then config validation rejects it with reason "allocation_unverified"
  And total_credit and short_premium remain selectable
  And no runtime toggle exists that lifts the gate

Scenario: Allocation reconciliation is recorded on every real fill
  Given a condor fill from the live broker under any stop_basis
  Then a reconciliation record is logged comparing sum of allocated leg prices to the net fill
  And the record PASSES only if they agree within one tick and no leg is zero-priced without trading at zero
  And paper-mode fills never produce reconciliation records

Scenario: Ungate criterion is fixed
  Given fewer than 5 consecutive PASSED reconciliation records from real fills
  Then the gate cannot be lifted
  And a FAILED record resets the consecutive count to zero
  And lifting the gate requires an operator-ratified spec amendment
```

**TC-STP-13** — STP-05a (contract test, sandbox): document the observed trigger source (last trade vs NBBO/mark) for a single-leg SPXW stop; test fails with an actionable message if single-leg option stops are rejected — build MUST NOT proceed past this failure.

## Net-loss estimation (NLE)

**TC-NLE-01** — NLE-01 computation correctness
```gherkin
Scenario: Chain-implied estimate matches hand computation
  Given a scripted put-side chain:
    | strike | mid  |
    | 5990   | 1.35 |   # short
    | 5960   | 3.10 |
    | 5950   | 4.20 |
    | 5945   | 5.14 |   # = trigger => D = 45
    | 5940   | 0.15 |   # long (fill)
    | 5985   | 1.55 |   # long strike shifted 45 ITM = 5940+45
  And stop trigger = 5.14 and nle_haircut_pct = 30
  Then implied move D = 45
  And raw long estimate = 1.55, haircut estimate = 1.085
  And estimated net loss = (5.14 - 1.35) - (1.085 - 0.15) = 2.855
  And it is reported in $ and as % of the stop-basis credit
  # interpolation asserted separately with a trigger falling between strikes
```

**TC-NLE-02** — NLE-02: asymmetric scripted chain (put skew) produces different put and call estimates; no blended figure exists anywhere in the API payloads.

**TC-NLE-03** — NLE-03: stale chain / too few strikes to interpolate ⇒ estimate UNAVAILABLE; the entry proceeds normally and on time; no alert above info level.

**TC-NLE-04** — NLE-04 isolation (property test): across stop_basis × pct ∈ {95..300 step 5} × NLE {enabled, disabled, throwing}, stop trigger prices and stop order payloads are byte-identical. The estimator module has no import path to order placement (architecture test).

**TC-NLE-05** — NLE-06: every short-stop event (including whipsaw — both sides) writes a complete calibration record; estimate error = realized − estimated; record appears in the EOD-05 report; replay (TC-REC-01) reproduces identical records.

**TC-NLE-06** — NLE-07: with 24 samples the calibration view reports "insufficient data"; with 25+ it reports per-side realized net-loss ratio and mean estimate error matching hand-computed values from the scripted history.

**TC-NLE-07** — NLE-05/UI-13: preview endpoint returns per-side estimates for a candidate pct when the market is open, UNAVAILABLE when closed/stale; changing pct in the selector recomputes without submitting anything.

## Long exit (LEX)

**TC-LEX-01** — LEX-01/LEX-03
```gherkin
Scenario: Ladder from mid toward bid
  Given the short put stop filled and the long put quotes bid 2.00 / ask 2.30
  Then a limit sell at 2.15 is placed within lex_start_latency_ms
  When lex_reprice_seconds elapses without fill
  Then the order is replaced at one tick lower recomputed from the CURRENT quote  # EC-LEX-05
  And after lex_reprice_attempts unfilled replacements the fallback places a marketable limit at the current bid  # LEX-05
```

**TC-LEX-02** — LEX-02/EC-LEX-01: stale then crossed quotes ⇒ wait, then fallback path; no order priced off invalid quotes.

**TC-LEX-03** — LEX-04 floor: sell price never below max(bid, intrinsic); scripted deep-ITM long asserts intrinsic floor binds.

**TC-LEX-04** — LEX-06/EC-LEX-02: fallback unfilled ⇒ critical alert and retry loop continues until fill or close.

**TC-LEX-05** — LEX-07: after LEX completes, per-side position is flat; no cheap-long is ever retained.

**TC-LEX-06** — LEX-08/EC-LEX-03: fill-during-replace race ⇒ broker truth adopted; double-fill (short position created) ⇒ immediate buy-back + critical alert.

**TC-LEX-07** — LEX-09: late fill after presumed cancel ⇒ P&L corrected from broker records.

**TC-LEX-08** — EC-LEX-04: zero-bid long ⇒ minimum-tick limit rests until fill or expiry.

**TC-LEX-09** — EC-LEX-06 restart mid-ladder: ladder resumes from persisted step, working order rediscovered by key.

## Take-profit floor (TPF)

**TC-TPF-01** — TPF-02 level availability (the user's canonical examples)
```gherkin
Scenario: Credit $4.00, profit $1.00 (25%)
  Then enabled levels are exactly {5, 10, 15, 20}
  And 25 and above are disabled with reason "too close - would trigger immediately"

Scenario: Credit $4.00, profit $3.00 (75%)
  Then enabled levels are exactly {5, 10, ..., 70}
  And 75 and above are disabled

Scenario: Profit 23%
  Then the highest enabled level is 15   # 20 violates the 5-point gap (23 - 20 < 5)
```

**TC-TPF-02** — TPF-02/UI-15 live validation, two layers:
```gherkin
Scenario: Selector revalidates continuously while open
  Given the selector is open with profit 25% and level 20 enabled
  When streamed profit falls to 24%
  Then level 20 greys out in place without reopening the selector  # 24 - 20 < 5

Scenario: Backend is authoritative at arm time
  Given the client submits level 20 based on its rendered profit of 25%
  But the backend's own mark computes profit at 22%
  Then the request is rejected (not clamped) and the UI refreshes  # EC-TPF-04
```

**TC-TPF-03** — TPF-01/03 trigger mechanics: floor 20% on $4.00 credit ⇒ close fires when profit marks ≤ $0.80 for tp_confirmation_evals consecutive valid evaluations; a single bad print does not fire; stale marks pause evaluation and reset the counter (EC-TPF-02).

**TC-TPF-04** — TPF-04/CLS-01 close procedure: stops cancelled and confirmed before spread close orders; close via reprice ladder with fallback; close-submit failure after stop cancel ⇒ stops re-placed and UNPROTECTED handling engaged.

## Canonical close (CLS)

**TC-CLS-01** — CLS-02 single path (the unification contract) + CLS-01 v1.50 replace-based close
```gherkin
Scenario: Manual close and TPF close are byte-identical
  Given two identical open entries A and B (same fills, same stops)
  When entry A is closed via the UI "Close trade" button
  And entry B is closed via a TPF floor trigger
  Then the sequence of broker requests (replaces, close orders, prices, quantities) is identical
  And only the recorded initiator differs: "manual" vs "take_profit"

Scenario: The close replaces stops, never cancels them bare
  Given an open entry with both stops resting
  When CloseEntry runs
  Then each short's stop is cancel/replaced with a marketable buy-to-close of ledger quantity
  And at no point does a short leg have zero working buy orders
  And at no point does a short leg have two working buy orders

Scenario: Replace races are terminal-safe
  Given the put stop fills while its replace is in flight
  Then the replace is classified FILLED (ORD-08a) and the side routes to SIDE_STOPPED + LEX
  And given the call replace fails transient
  Then the original call stop is still resting and the replace is retried per ORD-08

Scenario: No ad-hoc closes exist
  Then CloseEntry is the only module with close-order submission paths
  And no agent or tooling path can submit a broker order outside the application services
```
Also asserted architecturally: `CloseEntry` is the only module with close-order submission paths; Flatten All and EOD-02 route through it (initiators `manual_flatten`, `eod`).

**TC-CLS-02** — UC-14/UI-16: Close trade fires **instantly with no confirmation dialog** (Bug #16), closes via CLS, clears any armed TPF floor, tags the report `manual`; failures render as a toast, never a blocking dialog; a rapid double-click produces exactly one close (idempotency, TC-CLS-03). On a WORKING entry the action is Cancel entry (CLS-03), also instant. Flatten-all still requires the typed FLATTEN confirmation (TC-FLT-01).

**TC-CLS-03** — CLS-01(5) idempotency under retry: a duplicated close command (double-click, client retry) produces no duplicate orders; per-leg close order count = 1.

**TC-CLS-04** — CLS-01/05 completeness: after a Close trade on a fully-open entry, the broker holds zero positions and zero working orders for that entry's four legs (stops cancelled, both spreads closed); after Flatten all, the same holds for every bot entry while FOREIGN positions and the operator's own orders are untouched; a bot-commanded close never leaves a resting stop (contrast: broker-side operator interventions, TC-OWN-07/09, where the bot touches nothing).

**TC-FLT-01** — RSK-01a/UC-15 flatten all
```gherkin
Scenario: Flatten all with mixed entry states
  Given entry 1 OPEN (both sides), entry 2 with put side mid-LEX, entry 3 with a WORKING entry order, entry 4 OPEN with an armed TPF floor
  When the operator confirms Flatten all
  Then entry 3's order is cancelled (CLS-03), no close orders placed for its legs
  And entries 1, 2, 4 close via CloseEntry with initiator "manual_flatten"
  And entry 2's LEX ladder is superseded by an immediate marketable-limit close
  And entry 4's TPF floor is cleared
  And a scheduled entry arriving WHILE the flatten executes is SKIPPED (flatten_in_progress)
  And with the Stop Trading checkbox OFF, the next scheduled entry after completion fires normally into the clean book
  And with the checkbox ON, subsequent entries are blocked until reset (Stop Trading persisted across restart)
```

**TC-FLT-03** — RSK-01b combined control: one press (typed FLATTEN) activates Stop Trading BEFORE the first close order is submitted (event order asserted), then flattens every bot entry; afterwards no scheduled entry fires and the stopped state persists across restart; the implementation invokes the two existing controls (architecture assertion: no third close/block path exists).

**TC-FLT-02** — RSK-01a concurrency + rails: entries flatten concurrently; under injected 429s all flatten orders are exit-priority (EC-API-02); flatten orders are never blocked by the daily order cap (RSK-08).

**TC-TPF-05** — EC-TPF-03 race: short stop fills as its cancel lands ⇒ side routed to SIDE_STOPPED + LEX, other side closed by TPF; no duplicate buy-back (assert order count per leg = 1).

**TC-TPF-06** — TPF-05 partial scope: put side already stopped (realized −$1.10), call side open ⇒ profit% includes realized; trigger closes call side only.

**TC-TPF-07** — TPF-08/EC-TPF-01 restart: floor armed, bot down while profit gaps below floor ⇒ on recovery close triggers immediately at current level; report shows floor vs realized.

**TC-TPF-08** — TPF-06/07/09: raise/lower/clear all evented and gap-validated; floor never self-adjusts as profit grows (no trailing); Stop Trading leaves TPF active, an executing Flatten All supersedes; TPF trigger near close works until EOD per EOD-04.

## Decay buyback (DCY) & cancel taxonomy

**TC-ORD-06** — ORD-08 cancel-failure taxonomy (Bug #5 regression)
```gherkin
Scenario: Terminal cancel failure never retries (the all-night spam bug)
  Given a resting stop whose cancel fails with "order no longer exists"
  Then the order is marked dead and tracking stops
  And the cancel is never retried and protection is never re-added for a dead order
  And total requests for that order after the terminal response = 0

Scenario: Transient failure retries bounded, filled routes to fill handling
  Given cancels failing with timeouts, then a cancel rejected because filled
  Then the timeout case retries with backoff up to its cap
  And the filled case is handled as a fill (EC-API-06)

Scenario: Unclassifiable failure escalates
  Given a cancel failure matching no known class
  Then it is treated as transient with a hard retry cap and raises an alert at the cap
```

**TC-DCY-01** — DCY-01/02/03 happy path: short's ask ≤ $0.05 for 2 valid evals ⇒ stop cancelled (confirmed) → limit buy at trigger → fill ⇒ side = SIDE_CLOSED_DECAY, P&L realized, **long retained** and expiring, its strike still occupied for STK-09.

**TC-DCY-02** — DCY-02(3) re-inflation guard: ask jumps to $0.30 before the buyback fills ⇒ buyback cancelled, resting stop re-placed and confirmed; unprotected time ≤ decay_unfilled_timeout_seconds; if the stop had actually FILLED during the window (classified per ORD-08a), the LEX path runs instead.

**TC-DCY-03** — DCY-01 gates: trigger uses the ASK only (bid/mid/last scenarios prove no other price fires it); no trigger from a single bad print; none after decay_cutoff_time (15:55 default); none for MANUAL/SUSPENDED entries; none while a Flatten All executes; nothing fires outside RTH (structural — no tracked shorts exist overnight). Under stop-trading mode: buybacks CONTINUE; and if a re-inflation-guard stop re-placement fails once while in stop-trading mode, the watcher suspends until stop-trading reset (scenario asserts suspension and resumption).

**TC-DCY-04** — DCY-02/CLS-02 architecture: the buyback routes through the canonical close service (short-only scope, initiator `decay`); the no-other-close-path assertion still holds; day report lists the close as `decay`.

## End of day

**TC-EOD-01** — EOD-01: untouched condor held to settlement; sides marked EXPIRED; settlement P&L computed against SET/close value.

**TC-EOD-02** — EOD-02: with eod_close_time set, all open sides closed via ladder before the deadline.

**TC-EOD-03** — EOD-03: after settlement/close, zero working orders remain; an uncancellable order produces a named critical alert.

**TC-EOD-04** — EOD-04/EC-STP-09: stop fills at 15:58 ⇒ LEX works until close; remainder expires.

**TC-EOD-05** — DAY-02/EC-ENT-13 half day: late entries skipped; EOD schedule uses 13:00 close.

## Risk & safety

**TC-RSK-01** — RSK-01/EC-RSK-02/03
```gherkin
Scenario: Stop Trading blocks entries and nothing else
  Given two open condors and one LEX ladder in progress
  When Stop Trading is activated
  Then no further entries occur
  And resting stops remain working
  And the LEX ladder continues            # risk-reducing work proceeds
  And TPF monitoring and the decay watcher continue

Scenario: Flatten All does not block trading (orthogonality)
  Given Flatten All is confirmed WITHOUT the Stop Trading checkbox
  Then every bot entry closes via CLS
  And the next scheduled entry fires normally into the clean book

Scenario: Stop Trading persists across restart
  Given Stop Trading was active
  When the bot restarts
  Then entries remain blocked until the operator resets ("Resume trading")
```

**TC-RSK-02** — RSK-02 tombstone (absence test): config containing `daily_max_loss`, `daily_loss_also_flatten` or `risk_eval_seconds` is REJECTED as unknown keys; a scripted heavy-loss whipsaw day produces NO automatic halt, flatten, or entry block — remaining composed entries run through their normal gates (documents the deliberately accepted behavior); no `daily_loss` initiator exists anywhere (architecture assertion).

**TC-RSK-03** — RSK-04: entry blocked when worst-case exposure would exceed max_day_risk; assert both exposure numbers computed as specified.

**TC-RSK-04** — RSK-05/EC-DAT-04: absurd order price and absurd inbound quote both rejected before any broker call.

**TC-RSK-05** — RSK-07/EC-RSK-06: injected clock drift blocks entries; existing management continues.

**TC-RSK-06** — EC-RSK-04: in paper mode, the live adapter is not instantiated (structural test — assert wiring, not flags).

**TC-RSK-08** — RSK-08 order cap: scripted day approaching daily_order_cap ⇒ new entries blocked at the buffer; a stop replacement and a LEX order submitted after the cap are NOT blocked; cancel/replaces counted as orders.

**TC-RSK-07** — EC-RSK-05 full recovery SLA
```gherkin
Scenario: Crash with open positions
  Given one open condor, one side mid-LEX, one working entry order
  When the process is killed and restarted
  Then within recovery_sla_seconds every short is covered by a confirmed resting stop
  And the LEX ladder has resumed
  And the stale entry order is cancelled (window elapsed)
  And zero duplicate orders exist at the broker
```

## Market data & API

**TC-DAT-01** — DAT-02/EC-DAT-01: silent staleness (connected, no ticks) detected per instrument; decisions blocked.

**TC-DAT-02** — DAT-03/EC-DAT-02/03: disconnect during entry ⇒ attempt aborted; during LEX ⇒ repricing frozen, limit stays working, resume/fallback on reconnect.

**TC-DAT-03** — DAT-04/EC-DAT-05/EC-ENT-09: halt blocks entries; no catch-up; ladders re-validate quotes on resume.

**TC-API-01** — EC-API-01/REC-06: token expiry renewed proactively; forced auth failure ⇒ backoff, entries blocked, alert.

**TC-API-02** — EC-API-02: under injected 429s, exit-side requests are always sent before queued entry-side requests; none dropped.

**TC-API-03** — EC-API-04: unknown broker position adopted; naked short protected (stop placed) before any other action; entries blocked until reconciled.

**TC-API-04** — EC-API-06: cancel-rejected-because-filled routed as a fill.

## Position ownership (OWN)

**TC-OWN-01** — OWN-03 foreign quarantine
```gherkin
Scenario: Unmatched position is never touched
  Given the broker reports short 1 SPX 6050 call with no matching bot order fill
  Then the position is marked FOREIGN
  And the bot never submits any order referencing 6050 calls (stop, close, or hedge)
  And it appears in no bot P&L or risk figure
  And a critical alert and persistent banner are raised

Scenario: Even a foreign naked short is alert-only
  Given the FOREIGN position is an unprotected naked short in a moving market
  Then the bot still submits no orders for it   # never guess operator intent
```

**TC-OWN-02** — OWN-03 attribution: crash orphan vs foreign — a position matching the bot's own fill records (order IDs in the event log) is adopted and managed (REC-03/04, stop re-placed); an identical-looking position with no matching fills is FOREIGN. Both in one scripted account.

**TC-OWN-03** — OWN-05 shared symbol
```gherkin
Scenario: Operator sells 1 more of the bot's short strike
  Given the bot is short 2 x 5990 put (ledger = 2, stops resting for 2)
  When the broker position becomes short 3 (foreign_delta = 1)
  Then a persistent shared-symbol warning is shown
  And the resting stops remain for exactly 2
  And a subsequent stop fill triggers LEX for exactly the bot's long quantity
  And a Close trade on that entry submits orders for exactly 2
```

**TC-OWN-04** — OWN-06 ledger shortfall: operator manually buys back 1 of the bot's 2 shorts (broker = 1, ledger = 2) ⇒ entry automation SUSPENDED, ledger written down via ForeignReduction, critical alert, no compensating orders; automation resumes only on acknowledgment.

**TC-OWN-05** — OWN-04 structural cap (property test): across all exit paths (stop placement, LEX, CLS, flatten), every outbound order quantity ≤ ledger(symbol) at submit time, under randomized foreign deltas; the cap lives in the single order-construction path (architecture assertion).

**TC-OWN-07** — OWN-09 external close (Bug #7 regression)
```gherkin
Scenario: Operator closes the condor in the tastytrade app
  Given entry 1 is OPEN with stops resting and was seen_open in the positions feed
  When the position disappears and the stop shows NOT filled, on two consecutive reconciles
  Then all automation for the side stands down (no LEX, no TPF, no EOD close)
  And the bot submits NO orders and does NOT cancel its own leftover stop   # operator owns all cleanup
  And the critical alert lists the leftover stop with its open-a-long consequence
  And the alert's one-click Cancel-stop action cancels it when (and only when) clicked
  And the side is marked CLOSED_EXTERNAL

Scenario: A real stop-out is never mislabeled
  Given the short is gone AND its stop order shows FILLED
  Then the normal stop-out path runs (LEX) and no external-close event is emitted
```

**TC-OWN-08** — OWN-09 guards (Bug #8 regression): a fresh fill whose position hasn't propagated (net 0 read seconds after stop placement) does NOT trigger external close — seen_open unmet and grace window unexpired; the stop is left resting; a single reconcile pass is never sufficient.

**TC-OWN-09** — OWN-10 partial reduction: operator buys back 1 of the bot's 2 shorts ⇒ entry SUSPENDED, ledger written down, **zero order actions by the bot** (the 2-lot stop is left untouched); critical alert spells out the over-buy chain ("trigger closes 1, OPENS 1 long"); one-click Resize-stop and Cancel-stop actions each work when clicked and are tagged as operator actions in the event log.

**TC-OWN-11** — OWN-03 never-block / RSK-03 genuine-mismatch / STK-09 foreign occupancy / RSK-04 scope (v1.49 shared-account)
```gherkin
Scenario: Pre-existing positions do not block arming or entries
  Given the broker account holds positions with no bot fills behind them
  When startup reconcile runs
  Then the positions are classified FOREIGN with a critical alert and persistent banner
  And arming succeeds and scheduled entries fire normally

Scenario: A genuine shortfall still blocks
  Given the bot ledger records 2 contracts of a symbol and the broker reports 1
  Then a ReconciliationMismatch is logged and RSK-03 blocks entries until reconciled

Scenario: Foreign-occupied strikes block both types
  Given a FOREIGN long at the put side's target strike
  When strike selection runs
  Then the strike is treated as blocked and the shift budget applies
  And a FOREIGN short at a candidate long strike also blocks (no stacking onto foreign lots)

Scenario: max_day_risk counts only the bot's book
  Given foreign positions of any size and no open bot entries
  When an entry whose worst case fits max_day_risk is attempted
  Then RSK-04 passes — the foreign book does not consume the ceiling
  And the buying-power gate still evaluates broker reality including the foreign book

Scenario: Never touch survives the whole day
  Given trading proceeds alongside FOREIGN positions all day
  Then no bot order ever references a foreign lot (OWN-04 caps at ledger)
  And EOD verification ignores foreign working orders it did not place
```

**TC-OWN-10** — OWN-11 operator cancels respected (Ash's override rule)
```gherkin
Scenario: Operator cancels the bot's stop in the app, keeps the position
  Given the stop order shows cancelled with no bot-initiated cancel in the event log
  Then the bot does NOT re-place it
  And the side is marked USER_UNPROTECTED with a critical alert
  And the UI banner offers a one-click Re-protect action which places a fresh stop when clicked

Scenario: Bot-side absence still auto-protects
  Given a short whose stop was never confirmed (crash before placement)
  Then REC-04 re-places the stop automatically   # OWN-11 applies only to non-bot cancels
```

**TC-OWN-06** — OWN-07: flatten-all against an account containing FOREIGN positions closes every bot entry and leaves every FOREIGN position untouched; no account-level close-all endpoint is ever called (fake-broker records endpoint usage).

## Paper-mode simulation (SIM)

**TC-SIM-01** — SIM-02 fill model
```gherkin
Scenario: Touch does not fill; trade-through does
  Given a condor limit at 2.30 net credit
  When the real net mid touches 2.30 exactly
  Then the order does NOT fill
  When the natural price satisfies 2.30 OR the mid reaches 2.35 (one tick through)
  Then the order fills all-or-nothing with per-leg prices allocated from current quotes
```

**TC-SIM-02** — SIM-03 stop simulation: mark reaching the trigger fires the simulated stop; fill = trigger + 3 ticks; slippage recorded via the same EC-STP-03 path; LEX then runs identically to live.

**TC-SIM-03** — SIM-04 money: an entry fill posts credit minus per-leg fees to the ledger; open entries consume margin per the spread requirement and release on close; insufficient simulated BP skips the entry (rejected_bp); settlement posts against the real closing level; the ledger survives container restart (REC-07).

**TC-SIM-04** — SIM-05 pipeline identity: an identical scripted day through SimulatedBroker and FakeBroker(live-shape) produces the same event sequence, NLE calibration records, and a PNL-04 reconcile against the sim transaction ledger; every report stamped PAPER.

**TC-SIM-05** — SIM-01/06 structure + honesty: live adapter never constructed in paper (with TC-RSK-06); cert sandbox endpoints never referenced by paper mode (architecture assertion); the UI limitations tooltip renders the SIM-06 list.

## Runtime hardening (NFR)

**TC-NFR-01** — NFR-01 (Bug #4/#17 regression)
```gherkin
Scenario: A hung broker call cannot freeze the bot
  Given a broker REST call that hangs for 30 seconds (injected)
  When the next scheduled entry time arrives during the hang
  Then the entry attempt begins on time
  And the session probe, quote consumption and UI stream continue uninterrupted
```

**TC-NFR-02** — NFR-02: probe runs every session_probe_seconds during market hours only (zero probes outside); a probe hitting a token error triggers immediate refresh; proactive refresh occurs every session_refresh_seconds; the account stream survives a full simulated token rotation without losing a fill event.

**TC-NFR-03** — NFR-03: architecture test — no HTTP client is constructed without explicit timeouts; a warm-up primed against a black-hole endpoint aborts at its hard cap and the entry fires on schedule with gates deciding normally.

**TC-NFR-05** — NFR-05 (Bug #21 regression): a BOM-prefixed `.env` loads correctly (key names intact); a config file whose hash changed outside the bot's own writes blocks arming with a named-file error until operator confirmation; a truncated file is refused, never silently defaulted.

**TC-NFR-06** — NFR-06 (Bug #23): mutating request with a foreign Origin is rejected (403) even on localhost; WebSocket upgrade with foreign Origin refused; config with `bind_host` ≠ 127.0.0.1 and no `api_token` fails validation; with a token set, requests without the header are rejected and the documented curl fallback (with header) succeeds.

**TC-NFR-04** — NFR-04 (Bug #20 regression + adopted feed design)
```gherkin
Scenario: One connection all day
  Given a full simulated trading day
  Then the fake transport counts exactly 1 persistent connection in the happy path

Scenario: Zombie ticks never land (generation guard property test)
  Given the hub replaced socket generation 2 with generation 3
  When late ticks from generation 2 arrive interleaved with generation 3 ticks
  Then no generation-2 tick reaches the marks table and prices never move backwards in time

Scenario: Single writer (architecture test)
  Then only the hub manager writes the marks table
  And the one-shot fetcher's data path returns to its caller only

Scenario: Decision moment - demand-reconnect heals
  Given the hub is sick and in an 8s backoff wait when an entry fires
  When the demand-reconnect succeeds within feed_demand_reconnect_seconds
  Then the entry proceeds on the healed hub (no fetcher used)

Scenario: Decision moment - fetcher path
  Given the demand-reconnect fails
  Then a one-shot fetcher returns a chain snapshot directly to the entry attempt
  And the snapshot passes chain-integrity gates before any selection
  And the marks table is untouched by the fetcher

Scenario: Decision moment - give up safely
  Given demand-reconnect and fetcher both fail
  Then the entry skips "data_unavailable", a LEX ladder freezes with its limit still working, TPF/DCY pause, an informational alert fires, and everything resumes on heal
```

## Reconciliation & persistence

**TC-REC-01** — REC-01/UI-10: replaying the event log reproduces identical day state and P&L (property test across scripted days).

**TC-REC-02** — REC-02: divergent broker vs internal state ⇒ broker adopted, mismatch logged, RSK-03 gate applied.

**TC-REC-03** — REC-03: mid-day cold start with live positions re-attaches stops, schedules, ladders; missed entries skipped.

**TC-REC-04** — REC-05: startup scans broker orders by idempotency key before any submission.

## P&L & fees

**TC-PNL-01** — PNL-01/02: per-entry P&L for a scripted day (entry credit, one stop-out with slippage, long recovery, one side expired) matches hand-computed figures including fees to the cent.

**TC-PNL-02** — PNL-03: live marking uses mid, degrading to worst-of-bid/ask when stale; TPF evaluation never blocks on broker reporting availability.

**TC-PNL-03** — PNL-04 broker authority: scripted transaction history diverging from bot-computed realized P&L by $0.12 on one entry ⇒ day report shows the broker figure as authoritative, `PnlMismatch` flagged with both figures and the delta, alert raised; divergence at $0.03 (under tolerance) reconciles silently; a recurring per-contract divergence pattern is surfaced as a fee-model correction suggestion; intraday replay determinism (TC-REC-01) is unaffected by reconciliation results.

## UI / API contract

**TC-UI-01** — UI-03/04: backend rejects out-of-range config regardless of client; stop pct accepts exactly {95..300 step 5}.
**TC-UI-02** — UI-05/07/12: dashboard state contract (WebSocket messages) includes mode, kill state, protection state; UNPROTECTED emits a critical-banner message.
**TC-UI-03** — UC-08/UI-11: manual actions are tagged `manual` in the event log; automated management pauses for that entry until manual mode exit.
**TC-UI-04** — UC-10: mode switch requires flat book and takes effect next day.

---

**TC-STP-20** — STP-08a live stop-fill reaction chain (v1.61)
```gherkin
Scenario: Wakes carry no data and one path decides
  Given a push event and a poll tick arrive for the same fill
  Then exactly one decision path reads broker truth and the journal and acts once
  And the fill is processed exactly once regardless of wake source

Scenario: A sold long is never re-sold
  Given the journal shows the side's long already sold
  When any wake detects the historical stop fill again
  Then no order is placed and the wake is a no-op

Scenario: Poll skips when busy, push waits
  Given the decision path is mid-action
  Then a poll tick SKIPS (its next tick catches up) and a push WAITS for the lock

Scenario: Stream outage lifecycle
  Given the order-event stream drops
  Then reconnection backs off with a cap, exactly ONE alert fires for the outage
  And the fallback poll is authoritative until resumption re-arms push

Scenario: A decay buyback fill is never a stop-out
  Given a side's fill is identified as the DCY buyback rather than the stop
  Then the side classifies SIDE_CLOSED_DECAY and the long is left to expire
  And no LEX ladder starts
```

**TC-UI-07** — UI-18a/UI-28/UI-26a/UI-23a/RPT-09a display rules (v1.61)
```gherkin
Scenario: Entry money renders as position dollars with one consistency
  Given an entry with contracts = 2 and per-contract net credit 4.00
  Then displays show 800 dollars and side displays sum exactly to the total
  And aggregates sum per-entry dollars via the single aggregation path

Scenario: Exemptions stay native
  Then quoted prices, ticks, and trigger prices render per-share
  And slippage renders in both ticks and position dollars
  And no displayed cash number passes through binary float

Scenario: Markup dial discloses per row
  Given a schedule row sets stop_rebate_markup 0.50 with contracts 2
  Then the row shows the shortfall sentence AND "worst case rises by $200" (0.50 x 100 x 2 x 2)
  And out-of-grid values are rejected, never clamped

Scenario: Heatmap honesty
  Given an imported day and a day with no data
  Then the imported day shows its imported values and the empty day shows "no data"
  And a fabricated 0-0 never renders
  And weekends render visually distinct from zero-P&L trading days

Scenario: The local label is the browser's zone, not geolocation
  Then the echo label names the Intl-resolved zone and no location lookup ever occurs
```

## Results dashboard (doc 10)

**TC-RPT-01** — RPT-01/02/UI-25
```gherkin
Scenario: Period buckets and trust stamps
  Given fills across two ET days, one broker-reconciled and one pending
  Then Today shows only today's entries with a bot-computed badge
  And the month badge reads "1/2 days broker-confirmed"
  And paper fills never appear in live periods or exports

Scenario: Disarmed flat days do not dilute averages
  Given 5 trading days and 2 disarmed flat days in a month
  Then day-based means and win rates use n=5
```

**TC-RPT-02** — RPT-04 pinned return vectors
```gherkin
Scenario: The canonical five-day vector computes exactly
  Given capital base 10000 and daily nets +400, +20, -360, +400, +20
  Then ROC = 4.80 percent, annualized Sharpe = 4.79, max drawdown = 360 dollars (3.60 percent)
  And profit factor = 2.33, expectancy = +96 dollars per entry, day win rate = 80 percent

Scenario: Sharpe gates on sample size
  Given 19 trading days
  Then Sharpe and Sortino render "insufficient data" and ROC still renders
```

**TC-RPT-03** — RPT-03 outcome taxonomy & contract audit
```gherkin
Scenario: Outcomes classify exactly once and honor the v1.38 contract
  Given the 4.00-credit canonical trade stopped on the put side only
  Then the entry is ONE_SIDE_STOPPED with realized >= +20 dollars minus recorded slippage
  And a both-sides day classifies BOTH_SIDES_STOPPED with realized >= -360 dollars minus recorded slippage

Scenario: A contract breach flags red
  Given a ONE_SIDE_STOPPED entry whose realized loss exceeds the recorded slippage allowance
  Then the dashboard renders a contract-breach flag with a drill-down to its fills
```

**TC-RPT-04** — RPT-05/06/07 decomposition
```gherkin
Scenario: Targeting decomposition separates causes
  Given target 3.00, matched probe 2.95 at probe number 2, short filled 2.93
  Then selection gap = -0.05, execution gap = -0.02, probe depth = 2

Scenario: Slippage-in can be positive
  Given first-rung credit 3.50 and fill credit 3.60
  Then slippage-in = +0.10 price improvement

Scenario: Stop slippage reports from EC-STP-03 records
  Given a stop with trigger 3.80 filled at 3.90
  Then slippage-out = 0.10 = 2 ticks and it enters the mean and p90
```

**TC-RPT-05** — RPT-10 determinism
```gherkin
Scenario: Replay reproduces the dashboard
  Given any event log
  When the log is replayed from genesis into a fresh projection
  Then every dashboard number is byte-identical to the incremental projection
```

**TC-RPT-06** — RPT-01 read-only isolation
```gherkin
Scenario: The reporting module cannot trade
  Then the reporting module has no order-action dependency on the broker gateway
  And no /reports endpoint can mutate trading state
  And its only broker access is the RPT-15 read-only reconciliation fetch
```

**TC-RPT-07** — RPT-11 waterfall reconciliation
```gherkin
Scenario: The waterfall reconciles to the cent
  Given a period with credits 8400, stop costs 2600, recoveries 310, buybacks 145, fees 220, slippage 95
  Then the waterfall bars sum exactly to the period net of 5650
  And premium capture ratio = 67.3 percent
  And any nonzero attribution residual renders an error state, never a silently adjusted bar
```

**TC-RPT-08** — RPT-12/13 excursions & slots
```gherkin
Scenario: MAE measures trigger-distance consumed
  Given a short filled at 3.00 with trigger 3.80 whose recorded mark peaked at 3.60 before expiry
  Then the entry MAE = 75 percent of trigger distance and it counts as survived
  And missing samples render as gaps, never interpolated

Scenario: Slot analytics attribute to the scheduled slot
  Given entries fired from the 10:00 and 12:35 slots across a month
  Then win rate, expectancy, and premium capture render per slot
  And manual entries group under a "manual" slot
```

**TC-RPT-09** — RPT-15 EOD broker reconcile-and-correct (operator rule: zero drift)
```gherkin
Scenario: A matching day is stamped broker-confirmed
  Given the day's projected fills, cash delta, fees, and flat check match the broker
  Then the day is stamped broker-confirmed and UI-25 shows the tick

Scenario: A mismatch corrects to broker truth, never silently
  Given the broker reports fees 2.40 where the projection assumed 2.20
  Then a CorrectionRecord event enters the log storing both values and the diff
  And the dashboard renders the broker value with the correction visible in the drill-down
  And an alert fires and the RPT-08 correction count increments

Scenario: No dashboard number ever changes without a CorrectionRecord
  Then any divergence between rendered numbers and the projection fold is a test failure

Scenario: Broker unreachable never auto-confirms
  Given the EOD reconcile fetch fails
  Then the day remains bot-computed and reconciliation retries at the next boot or reconcile

Scenario: Settlement cash is included or the day cannot confirm (v1.59, real 2026-07-09 vector)
  Given 4 entry legs netting +355.12 and a short C7540 with SPX settling at 7543.64
  Then the journaled settlement event records -369.00 from the broker's Receive-Deliver records
  And the day's true net is -13.88 and only then may it stamp broker-confirmed
  And a reconciler reading trade transactions only MUST fail this scenario

Scenario: Settlement journaling is idempotent and never guesses
  Given the settlement backfill runs three times
  Then settlement records exist exactly once per attributable expiring symbol
  And an OWN-03-ambiguous symbol is withheld with reason "ambiguous_settlement"
```

**TC-TPT-01** — TPT-01→07 take-profit target (v1.58, vectors = operator's worked examples)
```gherkin
Scenario: Target fires on the way up through the canonical close
  Given an entry with actual net credit 4.00 and take-profit target 60 percent
  When whole-entry profit holds at or above 60 percent for 2 consecutive valid evaluations
  Then CloseEntry runs with initiator "take_profit_target"
  And the order sequence is identical to a manual close of the same position

Scenario: A passed target is rejected, never acted on
  Given a live entry currently up 35 percent
  When the operator submits a target of 30 percent
  Then it is REJECTED with "target already passed - current profit 35%"
  And 40 percent is the lowest selectable target

Scenario: The target disarms permanently when any stop fills
  Given credit 4.00, target 5 percent, and the put stop fills at 3.80
  And the long put recovers 0.30 and the call side is closable for 0.20
  Then whole-entry profit is +30 dollars = 7.5 percent and NO close fires
  And the card shows the target as disarmed and the call side rides its resting stop

Scenario: Armed feedback shows dollars
  Given actual net credit 4.00 and target 60 percent
  Then the card shows "closes at debit <= 1.60" and "keep >= 240 dollars"

Scenario: Floor and target coexist
  Given a floor at 20 percent and a target at 70 percent on one entry
  Then rising to 70 first closes with initiator "take_profit_target"
  And falling to 20 first closes with initiator "take_profit"

Scenario: Never broker-resting
  Then no resting take-profit order ever exists at the broker
  And each short leg has at most ONE working buy order at all times

Scenario: Recovery order of operations
  Given the bot restarts on an entry whose put stop filled while it was down
  Then the synthesized stop event disarms the target BEFORE any target evaluation
  And a stop-free entry above target on recovery closes immediately
```

## Traceability matrix

| Rule/Edge | Tests | | Rule/Edge | Tests |
|---|---|---|---|---|
| DAY-01/02 | TC-EOD-05 | | STP-01/02 | TC-STP-01/02/03 |
| DAY-03 | TC-RSK-05 | | STP-03 | TC-STP-09 |
| DAY-04/05 | TC-REC-01, TC-UI-04 | | STP-04 | TC-STP-04 |
| ENT-01/02 | TC-ENT-01 | | STP-02b/02c/03b / STP-05/05a | TC-STP-14/15/16/17, TC-STP-08/13 |
| ENT-03 | TC-ENT-02 | | STP-06 | TC-STP-01 |
| ENT-04/05 | TC-ENT-03 | | STP-07/08 | TC-STP-06 |
| ENT-06 | TC-ENT-04 | | STP-09 | TC-EOD-01 |
| ENT-07 | TC-ENT-05 | | LEX-01→09 | TC-LEX-01→09 |
| ENT-08 | TC-ENT-06 | | ENT-01a | TC-ENT-07 |
| ENT-09 / UI-22 | TC-ENT-08 | | ENT-01b | TC-ENT-07 |
| ENT-10 / UI-24 | TC-ENT-10, TC-UI-06 | | DAY-06 / UI-23 | TC-DAY-06, TC-UI-05 |
| DAY-01a / ENT-10(7) / UI-24a | TC-DAY-07 | | | |
| ENT-09b (manual floors) | TC-ENT-09 | | | |
| STK-10 (baseline v1.55) | TC-STK-09 | | ENT-08/09 (baseline capture) | TC-STK-09 |
| NLE-01→07 | TC-NLE-01→07 | | UI-13/14/15 | TC-NLE-07, TC-STK-02, TC-TPF-01 |
| TPF-01→09 | TC-TPF-01→08 | | EC-TPF-01→05 | TC-TPF-02/03/05/07/08 |
| CLS-01→05 | TC-CLS-01→04, TC-TPF-04 | | UI-16 / UC-14 | TC-CLS-02 |
| ORD-08 | TC-ORD-06 | | DCY-01→04 | TC-DCY-01→04 |
| ORD-09 | TC-ORD-07 | | STP-01 (qty invariant) | TC-STP-04 |
| ORD-09 (fill credit) | TC-ORD-08 | | STP-02 (actual fill) | TC-STP-19 |
| RSK-01a/01b | TC-FLT-01/02/03 | | UI-17/20 / UC-15 | TC-FLT-01/03 |
| OWN-01→11 | TC-OWN-01→11 | | EC-API-04 (rev.) | TC-OWN-01/02/04 |
| RSK-03 (genuine mismatch) | TC-OWN-11 | | STK-09 (foreign) | TC-OWN-11 |
| NFR-01→06 | TC-NFR-01→06 | | SIM-01→06 | TC-SIM-01→05 |
| RPT-01→15 / UI-25/26/27 | TC-RPT-01→09 | | RPT-15 (zero drift) | TC-RPT-09 |
| TPT-01→07 | TC-TPT-01 | | STP-08a | TC-STP-20 |
| UI-18a/23a/26a/28 / RPT-09a | TC-UI-07 | | | |
| STK-01→11 | TC-STK-01→08 | | EOD-01→05 | TC-EOD-01→05 |
| ORD-01→07 | TC-ORD-01→05, TC-ENT-05 | | RSK-01→08 | TC-RSK-01→08 |
| DAT-01→05 | TC-DAT-01→03 | | REC-01→06 | TC-REC-01→04, TC-API-01 |
| PNL-01→04 | TC-PNL-01→03 | | UI-01→12 | TC-UI-01→04 |
| EC-ENT-01→13 | TC-ENT-01/02/04/05, TC-STK-02/03, TC-ORD-01/03/04/05, TC-EOD-05 | | EC-STP-01→10 | TC-STP-04→12 |
| EC-LEX-01→07 | TC-LEX-01→09 | | EC-DAT-01→05 | TC-DAT-01→03, TC-RSK-04 |
| EC-API-01→06 | TC-API-01→04, TC-ORD-02 | | EC-RSK-01→06 | TC-RSK-01/02/05/06/07 |

**CI rule:** a script parses docs 01/02 for IDs and this matrix + test suite for coverage; any uncovered ID fails the build.
