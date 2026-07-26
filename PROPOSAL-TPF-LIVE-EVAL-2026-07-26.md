# Proposal — TPF/TPT live evaluation (draft v1.94)

**Date:** 2026-07-26
**Against:** `main` @ `8cc171d` (spec v1.93)
**Status:** **PROPOSAL ONLY.** `spec/` untouched, no code written. Text below is drafted to be ratified verbatim, edited, or rejected. **One open ruling (§6) must be decided before the agent starts** — it is a money-affecting choice and I have not baked in an answer.

---

## 1. The defect

The take-profit floor and target are evaluated **once every ~60 seconds**, not on the live quote stream.

Evidence, all on `main` @ `8cc171d`:

- `_evaluate_exits_once` is called from exactly one place — `_probe_once`, inside the health loop (server.py:3127). There is no other caller and no quote-push path.
- The health loop is `while True: await asyncio.sleep(health_interval_s); await _health_tick(...)` (server.py:3229–3233), with `health_interval_s = float(env.get("MEIC_HEALTH_INTERVAL_S", "60"))` (server.py:3222). Sleep-first, so the true period is 60 s **plus** the tick's own work (session probe, data probe, mark sampling, exit evaluation — all awaiting the network).
- DXLink ticks *do* arrive continuously: server.py:1695 runs `async for q in feed.quotes(...)` and calls `hub.apply_tick(...)`. That consumer **only writes marks**. Nothing subscribes the exit evaluation to it.

**Consequences.** A breach that begins and ends inside one 60-second window is never observed — not "seen and unconfirmed", never sampled. A breach that persists is acted on 60–120 s after it starts, before the CLS-01 ladder even begins. And `tp_confirmation_evals = 2`, written to mean "two adjacent streaming prints, so one bad tick can't fire it," silently became "the breach must persist for two minutes" when the evaluation landed on a poll loop. **Nobody changed the parameter; the ground moved under it.**

**This is not a spec gap.** TPF-03 already says monitoring runs *"on every valid quote evaluation."* The code does not do what was ratified. The amendment below exists to make the requirement unambiguous and un-driftable, not to change the intent.

Related history: NFR-04 (2026-07-13) fixed this half-way. It made the **marks** live and sub-second (hub-first resolution in `_resolve_leg_mid`) after measuring a mark frozen past 50 s. It left the **decision** on the 60 s tick.

---

## 2. What is NOT changing

The agent must not touch these while implementing. Listed explicitly because they are adjacent and tempting:

TPF-01 (profit% definition and the credit-percentage floor), TPF-02 (level set and the `tp_gap_pct` gap rule, reject-never-clamp), TPF-04 (canonical close via CLS-01, initiator `take_profit`), TPF-05 (scope: remaining open sides; stopped sides contribute realized P&L), TPF-06 (operator raise/lower/clear), TPF-07 (no auto-trailing), TPF-08 (persistence and immediate fire on recovery), TPF-09 (Stop Trading leaves TPF active; Flatten supersedes), TPT-01/02/03 (target definition, placement, mirror gap rule), **TPT-05 (permanent disarm on any stop fill)**, TPT-06/07, CLS-01/CLS-02 (one close path), EC-TPF-03/04/05.

---

## 3. Proposed amendment text — v1.94

> **TPF-03 Bot-monitored, never broker-resting (AMENDED v1.94).** A spread-profit floor is not expressible as a resting order; monitoring runs bot-side against the live quote stream. **The UI MUST state that the floor is active only while the bot is running AND able to mark the position** (UI-15, widened by TPF-03d). Evaluation cadence, confirmation, cost and failure surfacing are governed by TPF-03a–d below.
>
> **TPF-03a Evaluation cadence — dedicated owner, bounded interval.** Exit evaluation (floor and target) MUST be owned by a loop or subscriber whose ONLY duty is exit evaluation, and MUST evaluate every armed entry at least once per `config.exit_eval_interval_ms` (default 250). It MUST NOT be a duty of the health loop, or of any loop whose primary purpose is something else. **The rule constrains the OUTCOME, not the mechanism:** a dedicated short-interval loop and a tick-driven subscriber both satisfy it, and moving from one to the other later is an implementation change, not an amendment. Rationale recorded (2026-07-26): coupling exit evaluation to the 60 s health tick made any breach shorter than a minute unobservable, and made a persisting breach act 60–120 s late. Precedent: EC-STP-06 stop-fill catch-up was moved off this same tick onto its own loop for the same reason — one owner per concern.
>
> **TPF-03b Confirmation is a DURATION, never a count.** A trigger fires when the entry is breached on a valid evaluation AND has been continuously breached for at least `config.tp_confirmation_ms` (default 500). A recovery above the level, or any invalid evaluation (TPF-03/EC-TPF-02), CLEARS the elapsed time — it never pauses and resumes. `tp_confirmation_ms = 0` fires on the first valid breach. **`tp_confirmation_evals` is TOMBSTONED**: the config loader REJECTS the key, absence-tested, per the STP-03 / DAT-04a retire-don't-build precedent. Rationale: a count silently re-denominates itself whenever the evaluation cadence changes — "2" meant two adjacent prints and became two minutes with no edit to any config. A duration cannot be silently reinterpreted by a future cadence change. The default 500 ms at the default 250 ms interval restores the original intent (a second observation rejects a single bad print).
>
> **TPF-03c Evaluation cost invariant.** An evaluation pass MUST NOT perform work proportional to the day's event count when the event log has not changed. The day projection is folded at most once per unchanged log — cached and invalidated on append, or equivalent. Rationale: `domain.projection.fold` is a full replay of the event log; at sub-second cadence an uncached fold is O(evaluations × events) and degrades as the day grows, so it would pass in the morning and fail in the afternoon. Pinned by a test that counts fold invocations across repeated passes with no new events.
>
> **TPF-03d Unevaluable armed exits are SURFACED, never silent.** An armed floor or target whose entry has been unevaluable (stale snapshot, or any open side that cannot be fully marked) continuously for `config.exit_unevaluable_alert_s` (default 60) MUST raise an RSK-06 alert naming the entry and the reason, and MUST show a distinct state on the entry card. Rationale (NFR-09 shape): an unmarkable leg makes `_open_side_costs` return None, which reads as "no breach" — indistinguishable from "not breached" to every consumer, so the operator believes a floor is protecting them when it has not been evaluated for hours.
>
> **TPT-04 (AMENDED v1.94).** The target is evaluated bot-side only, by the SAME evaluator, on the SAME dedicated owner, under TPF-03a–d in full — one evaluator, one loop, one confirmation rule. The UI carries the same UI-15/TPF-03d warning. Everything else in TPT-04, and TPT-05's permanent disarm, is unchanged.
>
> **EC-TPF-02 (AMENDED v1.94).** Evaluation pauses on stale quotes (DAT-02) and sanity-rejected ticks (EC-DAT-04); the elapsed breach time is CLEARED (TPF-03b), not paused; no trigger fires on invalid data.
>
> **NFR-08a Exit evaluation failures alert.** A raised exception from the exit evaluation pass MUST produce a CRITICAL alert, rate-limited to no more than one per `config.exit_unevaluable_alert_s` per distinct error, and MUST NOT be reported by log line alone. Rationale: the current call site catches everything and emits `logger.warning` only, so a throwing evaluator leaves every exit dead for the session with no operator-visible signal — the exact incident behind v1.90's NFR-08 ("an exit evaluator threw on 15 consecutive health ticks while all exits were dead and the session continued"). At a 250 ms cadence a silent throw is 240× more frequent and no more visible.
>
> **TPF-03e Honesty about what a floor can deliver.** Profit% is computed from MIDS, and the close runs through the CLS-01 reprice ladder. A faster evaluation reduces detection latency; it does not make execution instantaneous, and on a fast move the realized level WILL be worse than the floor. The UI and doc 12 MUST describe the floor as a giveback control, never as a guaranteed exit price. The broker-resting short stops (STP-01) remain the risk control; the floor is a profit control.

---

## 4. Config changes (doc 06, TPF section)

```
| `exit_eval_interval_ms`     | 100–5000  | 250 | immediate | TPF-03a — max interval between exit evaluations (floor + target) |
| `tp_confirmation_ms`        | 0–10000   | 500 | immediate | TPF-03b — breach must hold continuously for this long; 0 = fire on first valid breach |
| `exit_unevaluable_alert_s`  | 5–600     | 60  | immediate | TPF-03d / NFR-08a — armed exit unevaluable this long ⇒ RSK-06 alert |
| ~~`tp_confirmation_evals`~~ | —         | —   | —         | RETIRED v1.94 (TPF-03b — a count silently re-denominates on any cadence change); config loader rejects the key |
```

`tp_gap_pct` unchanged. Floor levels {5..90 step 5} remain fixed by TPF-02.

---

## 5. Test cases — TC-TPF-09 (new), TC-TPT-02 (new)

```gherkin
# TC-TPF-09 — TPF-03a/b live evaluation. Credit 4.00, floor 20% ⇒ breach at profit ≤ 0.80.
# Clock is the FakeClock; exit_eval_interval_ms 250, tp_confirmation_ms 500.

Scenario: A sub-minute breach is caught (regression guard for the 60s defect)
  Given an armed floor of 20 percent on an entry with net credit 4.00
  And profit falls below the floor at t=0ms and recovers at t=700ms
  When the exit evaluator runs at its configured interval
  Then evaluations occur at t=0, 250 and 500ms
  And CloseEntry runs with initiator "take_profit" at t=500ms
  # Under the retired 60s health-tick cadence this breach was never observed at all.

Scenario: A breach shorter than the confirmation duration does not fire
  Given profit falls below the floor at t=0ms and recovers at t=300ms
  Then no close fires
  And the elapsed breach time is cleared on the recovery

Scenario: An invalid evaluation clears the elapsed time, never pauses it
  Given profit is below the floor from t=0ms onward
  And the evaluation at t=250ms is invalid (stale snapshot or an unmarkable open side)
  Then no close fires at t=500ms
  And the close fires at t=1000ms   # the timer restarted at t=500, not resumed

Scenario: Confirmation of zero fires on the first valid breach
  Given tp_confirmation_ms is 0 and profit is below the floor at t=0ms
  Then CloseEntry runs with initiator "take_profit" at t=0ms

Scenario: The projection is not re-folded per evaluation
  Given an armed floor and an event log that does not change
  When 20 evaluation passes run
  Then domain.projection.fold is invoked at most once

Scenario: An armed floor that cannot be evaluated is surfaced
  Given an armed floor whose entry has an open side that cannot be fully marked
  When that condition persists for exit_unevaluable_alert_s
  Then an RSK-06 critical alert names the entry and the reason
  And the entry card shows the exit as unevaluable, distinct from armed-and-healthy

Scenario: A throwing evaluator alerts, it does not merely log
  Given the exit evaluation pass raises
  Then a CRITICAL alert is raised naming the error
  And repeat alerts for the same error are rate-limited to one per exit_unevaluable_alert_s
  And the evaluation loop survives the exception

Scenario: The retired count key is rejected
  Given a config containing tp_confirmation_evals
  Then the config loader REJECTS it   # absence test, TPF-03b tombstone
```

```gherkin
# TC-TPT-02 — TPT-04 parity on the rising edge, same evaluator and loop.

Scenario: A target is caught on a sub-minute spike
  Given an armed target of 60 percent on an entry with net credit 4.00
  And profit rises above the target at t=0ms and falls back at t=700ms
  Then CloseEntry runs with initiator "take_profit_target" at t=500ms

Scenario: The disarm still wins
  Given an entry whose put stop has filled
  And profit rises above the armed target
  Then no close fires at any cadence   # TPT-05 unchanged
```

---

## 6. OPEN RULING REQUIRED — mark freshness for a fast evaluation

**This one is yours, and the agent must not improvise it.** A faster loop is only as live as the marks it reads.

`_resolve_leg_mid` prefers the hub mark if it is fresher than `max_quote_age_ms` (3000). Otherwise it falls back to `_leg_mid` on the REST chain snapshot — **which is refreshed on the 60 s health tick.** So at a 250 ms cadence, a leg that has not ticked within 3 s is evaluated against a mark that may be up to a minute old. The loop would be fast; the data behind part of it would not.

This matters most for the long wings. A 45–55 point OTM long on 0DTE routinely goes quiet for long stretches.

**Option A — strict (fail-closed).** An exit evaluation may only use marks fresher than `max_quote_age_ms`. Any leg without one makes the entry unevaluable for that pass, feeding TPF-03d's alert. Honest and consistent with DAT-02 and NFR-09. Cost: on thin wings the floor may be unevaluable a large fraction of the time — the operator is told, but the floor genuinely protects less than it appears to today.

**Option B — tie the snapshot to the loop.** Refresh the chain snapshot at the exit cadence rather than the health cadence. Removes the staleness, at the cost of a much higher REST call rate against the broker — an API-budget question (EC-API-02 reserves capacity for exits) and likely a rate-limit problem.

**Option C — asymmetric freshness (my recommendation, flagged as a judgement call).** Require the SHORT leg of each open side to be fresh within `max_quote_age_ms`; allow the LONG leg a longer budget (`exit_long_leg_max_age_ms`, suggested 30000), because the short dominates cost-to-close and a quiet far-OTM long moves profit% very little. Fails closed on the leg that matters, stays evaluable in the common case. **This is a money-affecting approximation and should be ratified explicitly or rejected — I am not treating silence as consent.**

Until this is ruled, the agent implements TPF-03a–e with the CURRENT mark resolution unchanged, and the ruling lands as a follow-up.

---

## 7. Required verifications before implementation (report, do not fix)

1. **DCY cadence.** DCY-01 states the decay watcher is "event-driven on tracked shorts' quotes", but the implementation runs `_run_decay_watcher_loop`, a supervised polling loop (server.py:2112). Report its actual interval and whether a $0.05 ask trigger can be missed between passes. If it has the same shape of defect, it is a separate amendment — do not fold it into this one.
2. **Whether the friend's incident is explained by this defect at all.** Two non-bugs must be excluded first: (a) if a **target** was armed and any short stop filled, TPT-05 disarms it permanently and nothing firing is CORRECT (pinned vector: credit 4.00, target 5%, put stopped, profit +7.5%, nothing happens); (b) if the backend rejected the arm on TPF-02's gap re-validation (EC-TPF-04), no floor ever existed. Establish which before claiming this fix resolves it.
3. **`MEIC_HEALTH_INTERVAL_S` in the affected deployment** — it is an env var and may not be 60 there.

---

## 8. Implementation sequence

1. Verifications in §7. Report before writing code.
2. Projection cache satisfying TPF-03c — **first**, because it is the safety precondition for any faster cadence.
3. Dedicated exit loop at `exit_eval_interval_ms`, evaluation moved off `_probe_once`. `_recover_exits_once` (TPF-08/TPT-07) stays where it is — it is a boot/reconnect path, not a cadence concern.
4. Duration-based confirmation; tombstone `tp_confirmation_evals` with its absence test.
5. NFR-08a alerting at the call site.
6. TPF-03d unevaluable surfacing + card state.
7. NFR-07 registry entry for the new loop — it is a spec-mandated live component and must be provably constructed and ticked in `live_app`, CI-gated.
8. Doc 12 update per DOC-01 (same-ratification duty), including TPF-03e's honesty wording.
