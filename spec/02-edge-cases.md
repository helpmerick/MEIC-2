# 02 — Edge Cases and Failure Scenarios

Each edge case has an ID, references the governing rule(s) in doc 01, and states the REQUIRED behaviour.
The coding AI MUST implement explicit handling (not incidental behaviour) for every case here, and every case maps to at least one test in doc 04.

---

## A. Entry edge cases (EC-ENT)

- **EC-ENT-01 Bot down at a scheduled entry time.** Window (ENT-02) elapsed ⇒ entry permanently skipped on recovery; logged with reason `missed_window`. Never execute a late entry.
- **EC-ENT-02 No strike satisfies delta target.** All strikes exceed `short_delta_max`, or chain lacks the wing strike (STK-02/03/07) ⇒ skip entry, reason `no_valid_strikes`.
- **EC-ENT-03 One side fails minimum credit.** Skip the entire entry (STK-05), reason `insufficient_credit`. Single-side entries are prohibited — never trade one spread of the condor alone.
- **EC-ENT-04 Both sides fail minimum credit / total below `min_total_credit`.** Skip entry (STK-05/06), reason `insufficient_credit`.
- **EC-ENT-05 Entry order never fills.** Reprice ladder exhausted at floor (ORD-03) ⇒ cancel, confirm cancel, skip entry, reason `unfilled_at_floor`.
- **EC-ENT-06 Entry order partially filled at cancel time.** A 4-leg complex order fills balanced per contract (ORD-01), so a partial fill means fewer *complete condors* than ordered — keep the filled condors (place their stops per STP-01) and record reduced quantity. If reconciliation ever reveals unbalanced legs (broker anomaly — should be impossible): try to complete the missing legs with a marketable limit for `config.partial_fix_seconds`; if not completed, flatten the filled legs at marketable limits. Never carry an unbalanced position past `config.partial_fix_seconds × 2`. Critical alert on any unbalanced discovery.
- **EC-ENT-07 Order rejected (margin/buying power).** Skip entry, reason `rejected_bp`; if two consecutive entries reject for BP, block remaining entries for the day (config `bp_reject_lockout`, default true) and alert.
- **EC-ENT-08 Order rejected (other broker error).** Retry once after `config.reject_retry_seconds`; second rejection ⇒ skip entry and alert.
- **EC-ENT-09 Market halted / limit-down at entry time.** DAT-04 ⇒ skip (no catch-up).
- **EC-ENT-10 VIX filter or blackout date.** ENT-06 ⇒ skip silently (info-level log, not an alert).
- **EC-ENT-11 Previous entry's order still working at next entry time.** ENT-07/ORD-06 ⇒ cancel previous first; resolve partials (EC-ENT-06) before starting the new attempt. If resolution consumes the new entry's window, the new entry is skipped.
- **EC-ENT-12 Fill confirmation arrives after cancel was sent (race).** Broker truth wins (LEX-08 principle). If the condor is actually filled, proceed as a normal fill: place stops (STP-01).
- **EC-ENT-13 Half day.** DAY-02 ⇒ entries too close to the 1 PM close are skipped; EOD schedule shifts to the early close.

## B. Stop edge cases (EC-STP)

- **EC-STP-01 Stop placement rejected or unconfirmed.** STP-04: retry loop, then `unprotected_action` (flatten side or condor) + critical alert. The position must never sit unprotected beyond the bounded retry period.
- **EC-STP-02 Bot crashes between condor fill and stop placement.** On restart, REC-04 detects a stopless short ⇒ place stops immediately (or run LEX if the short was somehow already closed). This window is the system's biggest inherent risk; it must be minimized (stops placed in the same event-handler turn as fill processing) and covered by an explicit test.
- **EC-STP-03 Gap through the stop.** Stop-market fills far beyond trigger. Accept the fill (that's the design trade-off, STP-03), record slippage = fill − trigger, alert if slippage > `config.slippage_alert_ticks`.
- **EC-STP-04 Both shorts of one condor stop the same day (whipsaw).** Normal flow (STP-08): each side independently runs LEX. Both losses appear in day P&L (PNL-03).
- **EC-STP-05 Stop fills partially.** Continue working the remainder as the broker does for stop-market; LEX for the long begins only when the short is fully closed, but the *quantity-matched* portion may start earlier if `config.lex_partial_start` (default false — simpler invariant: LEX starts on full short close).
- **EC-STP-06 Stop fill event lost (bot down / stream gap).** REC-02 on reconnect discovers the short is gone ⇒ synthesize the missed event, run LEX immediately for the (now aged) long.
- **EC-STP-07 Operator changes `stop_loss_pct` or `stop_basis` mid-day.** Applies to new entries only (STP-02). Resting stops unchanged unless operator uses UC-08 explicit modify, which is a cancel/replace per side with confirmation and UNPROTECTED handling if replace fails.
- **EC-STP-08 stop_limit configured and triggered-but-unfilled.** Watchdog escalation to market after `stop_limit_escalation_seconds` (STP-03). If the bot is down when this happens there is no protection — this residual risk is why `stop_market` is the default.
- **EC-STP-09 Stop triggers within seconds of expiration.** If the stop fills, run LEX with EOD-04 (work until close). If it hasn't triggered by settlement, EOD-01 applies.
- **EC-STP-10 Duplicate stop orders discovered (e.g. after crash-retry).** REC-05/ORD-04: cancel the surplus order(s); reconcile before any other action on that side.

## C. Long-exit edge cases (EC-LEX)

- **EC-LEX-01 Stale or crossed long quote at LEX start.** LEX-02 ⇒ wait `lex_quote_wait_seconds`, then fallback marketable-limit-at-bid (LEX-05).
- **EC-LEX-02 Ladder exhausted without fill.** LEX-05 fallback; then LEX-06 alert loop.
- **EC-LEX-03 Fill arrives while cancel/replace in flight.** LEX-08: broker truth wins; if the old order filled, abort the replace; if both somehow fill (double sell ⇒ short position), immediately buy back the excess at marketable limit + critical alert.
- **EC-LEX-04 Long bid is zero.** Floor rule (LEX-04) makes mid-based ladder meaningless; place limit at minimum tick, hold order working until fill or expiry (EOD-04). LEX-07 still satisfied at settlement (expires worthless = fully closed).
- **EC-LEX-05 Underlying keeps running while ladder walks down.** The long is *gaining*; ladder repricing uses fresh mid each step (not the original mid minus n ticks) so it follows the market up as well as down. Each replace recomputes from current quote.
- **EC-LEX-06 Bot restarts mid-ladder.** REC-03: rediscover the working sell order (idempotency key), resume ladder timing from persisted state.
- **EC-LEX-07 LEX sale rejected by broker.** Retry once; then fallback path; then LEX-06 alert loop.

## C2. Take-profit floor edge cases (EC-TPF)

- **EC-TPF-01 Bot down when profit crosses the floor.** No trigger occurs (TPF-03 is bot-side by design). On restart, TPF-08 evaluates reconciled profit and closes at the *current* level, which may be worse than the floor — recorded and displayed, never treated as an error.
- **EC-TPF-02 Stale/absurd marks during monitoring.** Evaluation pauses on stale quotes (DAT-02) and sanity-rejected ticks (EC-DAT-04); the confirmation counter resets; no trigger fires on invalid data.
- **EC-TPF-03 Short stop fills during the TPF close.** Cancel-rejected-because-filled (EC-API-06) ⇒ that side is SIDE_STOPPED, LEX runs for its long; the TPF close proceeds for the other side only. No leg is ever bought back twice — idempotency keys per side enforce it.
- **EC-TPF-04 Gap-violating set request.** Profit moved between UI render and click; backend rejects (TPF-02), UI refreshes enabled levels. Never clamp to the nearest legal level.
- **EC-TPF-05 Floor set, then Flatten All triggers.** Flatten All supersedes TPF (positions close anyway); Stop Trading leaves TPF active (TPF-09). Both orderings are evented distinctly so reports show which mechanism closed the position.

## D. Market-data edge cases (EC-DAT)

- **EC-DAT-01 DXLink stream silently stale (connected, no ticks).** Staleness is measured per-instrument by event age (DAT-02), not connection state. Stale ⇒ same handling as disconnected for decision-making.
- **EC-DAT-02 Stream drops during an entry attempt.** Abort the attempt (cancel any working entry order); resting stops unaffected.
- **EC-DAT-03 Stream drops during LEX ladder.** Freeze repricing; existing limit stays working; resume on reconnect; if disconnect exceeds `config.lex_fallback_timeout_seconds`, on reconnect skip straight to fallback with fresh quote.
- **EC-DAT-04 Absurd quote (bid/ask orders of magnitude off, negative, inverted).** RSK-05 sanity checks apply to *inputs* as well as orders: reject the tick, treat as stale.
- **EC-DAT-05 Exchange halt mid-day.** DAT-04. On resume, re-validate all quotes fresh before any ladder resumes.

## E. Broker/API edge cases (EC-API)

- **EC-API-01 Session token expiry mid-day.** REC-06 proactive renewal; on auth failure: backoff-retry, block entries, alert. Resting stops unaffected.
- **EC-API-02 Rate limiting (429).** Global client-side rate limiter with priority classes: exit/stop/flatten orders always outrank entries and queries. Backoff and retry; never drop an exit-side request.
- **EC-API-03 Order status unknown (timeout on submit).** The order may or may not exist. Do NOT blind-resubmit: query by idempotency key (ORD-04) until state is known, then proceed. This is the canonical duplicate-order trap.
- **EC-API-04 Broker reports a position the bot doesn't know (or vice versa).** Attribution first (OWN-03): if the position matches the bot's own recorded order fills, it is a crash orphan — adopt and manage per REC-03/04 (including protecting a stopless short, REC-04). If it does NOT match, it is FOREIGN — quarantine per OWN-03: never stop it, never close it, never count it; critical alert + persistent banner; **even a foreign naked short is alert-only**. If the broker shows less than the bot's ledger, apply OWN-06 (suspend entry, write down, acknowledge).
- **EC-API-05 tastytrade maintenance window / outage.** Same as EC-API-01 handling. Day report must flag any period where management was impossible.
- **EC-API-06 Cancel request rejected because order already filled.** Treat as fill (broker truth), route to the appropriate handler (EC-ENT-12 / EC-LEX-03).

## F. Risk/ops edge cases (EC-RSK)

- **EC-RSK-01 — REMOVED (v1.32).** The daily loss limit no longer exists (RSK-02 tombstone); there is no loss-triggered automation to have edge cases.
- **EC-RSK-02 Stop Trading / Flatten All during LEX ladder.** Stop Trading: LEX continues — it blocks new entries only. Flatten All: the LEX ladder is superseded by immediate marketable-limit closes of everything.
- **EC-RSK-03 Stop Trading state after restart.** Persisted (RSK-01); the bot starts with entries blocked and refuses them until the operator resets ("Resume trading"). A Flatten All, being one-shot, has no state to persist.
- **EC-RSK-04 Paper/live mode mismatch protection.** Live order endpoints must be structurally unreachable in paper mode (separate adapter wiring, doc 05), not guarded by an `if`.
- **EC-RSK-05 Machine reboot / process OOM mid-day.** Full REC-02/03/04 startup path; the acceptance bar is: within `config.recovery_sla_seconds` (default 60 s) of restart, every position is either protected by a confirmed resting stop or being actively flattened, and no duplicate orders were created.
- **EC-RSK-06 Clock drift discovered mid-day.** RSK-07: block entries; existing management (ladders, stops) continues — broker timestamps are used for sequencing where possible.
