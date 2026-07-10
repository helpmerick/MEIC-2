# Spec amendment proposal — RPT-16: one-time historical backfill (broker-imported days)

**To:** Ash (operator / spec owner)
**From:** coding agent
**Re:** importing pre-journal trading history into the reporting event log
**Status:** PROPOSED — operator requested in chat 2026-07-10 ("do a one time
ratified backfill"); built behind this proposal for formal ratification.

---

## Why

The durable event journal (REC-01, built 2026-07-10) starts at day zero =
2026-07-10. The live trades of **2026-07-09** happened while the event log was
in-memory only and were lost across that day's many restarts — so the Results
dashboard cannot render them, though the broker's records hold every fill.
The operator wants that day visible.

Doc 10 Principle 4 says every number derives from the event log — so imported
history must enter AS EVENTS, explicitly labeled, never as synthetic
`CondorFilled`s pretending the bot decided them (REC-02: the log is
authoritative for INTENT; imported rows have no recorded intent).

## Proposed rule — RPT-16 Broker-imported days

> **RPT-16 (one-time backfill, operator-triggered).** A day that predates the
> journal MAY be imported from broker records, subject to:
> 1. **A distinct event type** — `ExternalFillImported` (day, at, order_id,
>    symbol, action, quantity, price, imported_at, source) — one per broker
>    fill leg. Never `CondorFilled`; imported history is data, not intent.
> 2. **Bot's book only (OWN-03):** the import takes an EXPLICIT operator-
>    supplied list of bot/agent order ids. Foreign fills (the operator's own
>    trading) are never imported.
> 3. **Cash-level rendering only:** an imported day renders its fills, net
>    cash delta, and fees. It is EXCLUDED from strategy-quality metrics
>    (Sharpe/Sortino/expectancy/streaks/outcome taxonomy/targeting/slippage) —
>    entry-level intent (targets, probes, stops timing) was never recorded and
>    is not reconstructable honestly. It counts as a trading day in RPT-01.
> 4. **Trust badge `broker-imported`** (UI-25 third state): the day's numbers
>    ARE broker truth by construction — but it is never labeled
>    broker-confirmed (RPT-15 confirmation means bot-computed numbers MATCHED
>    the broker, which is meaningless here).
> 5. **Idempotent + auditable:** re-running the import for a day with existing
>    `ExternalFillImported` events is a no-op; the import itself is evented
>    (`imported_at`, source) and the endpoint is auth-gated (NFR-06).
> 6. **Read-only at the broker:** the import uses the RPT-15 read-only fetch
>    surface. No order capability.

## The one-time payload (2026-07-09 — operator-scoped, chat ruling 2026-07-10)

**Import exactly ONE order: 482390058** — the final MEIC of the day (filled
15:29 ET: P7535 short / P7510 long / C7540 short / C7565 long, broker-actual
net credit 3.60), which the operator held to expiry (EOD-01 cash settlement).

Explicitly EXCLUDED, per the operator ("only the last MEIC that was entered
yesterday that went to expiry, ignore the rest"):
- The day's earlier bot condors and their agent closes (482214732/482258280,
  482314017/482320472, 482330547/482331956, 482347963/482352202) — test
  trades, opened and closed within minutes.
- Its resting stops 482390098/482390131 (died at the bell unfilled — no fill
  to import) and every cancelled/rejected order.
- The operator's own trading (OWN-03), e.g. 482147293.

Settlement note — SUPERSEDED by operator ruling 2026-07-10: **settlements ARE
imported, always.** The original build imported only the entry's Trade fill
legs, which rendered 2026-07-09 as +$355.12 (entry credit) when the trade
actually LOST $13.88 — SPX settled ≈7543.64 and the short C7540 was
cash-settled-assigned for a net −$369.00 (value −364.00 + $5.00 fee). The
operator ruled that a P&L showing a losing trade as a winner is unacceptable,
so:

- The broker's Receive-Deliver transactions for the day (fetched with
  `end_date = day + 1`, since settlements can post next-day) are imported as
  `ExternalFillImported` events too: `action` = the broker's
  `transaction_sub_type` ("Cash Settled Assignment" / "Expiration" /
  "Assignment"), `value` = the broker's signed `net_value` (already
  net-of-fee real dollars — a new, additive event field, None on Trade rows),
  `fee` = `|net_value − value|`. Zero-value Expiration/Assignment removal
  rows are imported as well: they document the legs' terminal state and cost
  nothing.
- **Idempotency is now transaction-level**, not day-level: identity is
  `(at, symbol, action)` per already-recorded `ExternalFillImported` for the
  day. Re-running the import on a day imported fills-only ADDS exactly the
  missing settlement rows once; a further run is a true no-op. (The old
  day-level "already_imported" short-circuit could never pick up a settlement
  that posted after the first import.)
- **Shared-symbol ambiguity guard (OWN-03):** a settlement row carries no
  order id, so it is matched by SYMBOL against our imported orders' fill
  symbols. That attribution is only safe when the symbol was not ALSO traded
  by a foreign position that day — for 2026-07-09's order 482390058 all four
  symbols (P7535/P7510/C7540/C7565) are unshared, so the matching is exact.
  A settlement symbol that also appears in skipped-foreign fills is SKIPPED
  and counted in the endpoint's `ambiguous_settlements` field, never guessed;
  the general shared-symbol attribution fix needs a future ruling.

With settlements imported, 2026-07-09 renders 355.12 − 369.00 = **−13.88**
net with 9.88 total fees — the losing trade shows as a loss.

## Test cases (TC-RPT-10 suggested)
- Importing a day creates ExternalFillImported events only for the supplied
  ids; a foreign fill in the same day's history is not imported.
- Re-import is a no-op.
- The imported day renders fills + cash + fees with the broker-imported badge,
  appears in RPT-01 buckets, and contributes NOTHING to RPT-04 metrics or
  RPT-03 taxonomy.
- Replay-from-genesis reproduces the imported day byte-identically (RPT-10).
