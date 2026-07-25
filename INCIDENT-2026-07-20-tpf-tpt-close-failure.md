# Incident Report — TPF/TPT & Close failure (live), 2026-07-20

**Severity:** High (automated risk-management exits non-functional during live 0DTE trading)
**Status:** Root cause confirmed from logs. A same-day fix was applied then **reverted**, so the
defect is **present again on `main`** as of this report.
**Prepared for:** the meic-bot-2.0 developer.
**Evidence:** `evidence-2026-07-20.log` (curated, chronological) + raw logs
`logs/meic-20260720T*.log` and `logs/meic-20260719T170124Z.log`.
**Times:** machine local **MDT (UTC−6)**; ET = MDT + 2. Market hours 07:30–14:00 MDT (09:30–16:00 ET).

---

## 1. Executive summary

Throughout the 2026-07-20 session the bot could **not place any automated closing order**. Every
exit path that routes through `marketable_close()` — the **Take-Profit Floor (TPF)**, **Take-Profit
Target (TPT)**, the **manual Close button**, and **stop-escalation buybacks** — failed at broker
submission. The exit evaluator threw on **every health tick** (15 failures logged between
09:45 and 11:01 MDT). Because the bot could not close, **the operator had to manage positions by
hand in the tastytrade desktop app** (≥4 manual SPX stop/close fills observed).

Mid-session hot-fix attempts introduced **three further distinct failures** before the change was
reverted, and when market orders briefly did submit, an **idempotency/replay defect re-opened a
position** (a "Buy to Close" filled as **Buy to Open**). The resulting state desync caused the bot
to **quarantine its own SPX legs as "FOREIGN" (OWN-03)** — never stopped, closed, or counted.

## 2. Root cause

`marketable_close()` builds the order as **`order_type="marketable_limit"` WITH a `price`**:

- `backend/src/meic/application/order_intent.py:178-183` — returns `order_type="marketable_limit", … price=price`.

tastytrade **rejects a marketable-limit order that carries a price**:

> `order_must_omit_price: This order can not be submitted with an associated price.`

So the intent is structurally invalid for the broker. `marketable_close()` is the shared exit path
for CLS-01 legs, STP-03b escalation, DCY buy-back, and LEX long recovery — so **all** automated
closes and the manual Close button failed identically.

Call sites confirmed still on this path (post-revert):
- `backend/src/meic/application/close_entry.py:145` (manual / TPF / TPT closes)
- `backend/src/meic/application/watchdog.py:114` (escalation buybacks)
- `ORDER_TYPES` (`order_intent.py:32`) has no `"market"`; adapter type_map
  (`adapters/tastytrade/adapter.py:185`) maps only `limit` / `marketable_limit`.

## 3. Timeline of failure modes (all "health tick: exit evaluation failed")

| Window (MDT / ET) | Error | Cause | Count |
|---|---|---|---|
| 09:45–~10:03 (11:45 ET) | `TastytradeError('order_must_omit_price')` | original bug — marketable_limit **with price** | 3* |
| 10:08–10:12 (12:08 ET) | `IntentError("order_type 'market' not in […]")` | fix attempt 1: used `"market"` but never added it to `ORDER_TYPES` | 4 |
| 10:15–10:24 (12:15 ET) | `KeyError('market')` | fix attempt 2: adapter type_map has no `"market"` entry | 6 |
| 10:58–10:59 (12:58 ET) | `AttributeError('OrderType' has no attribute 'MARKETABLE')` | fix attempt 3: wrong enum (`OrderType.MARKETABLE` vs `.MARKET`) | 2 |
| 11:00–11:01 (13:00 ET) | market orders submit, but **BTC fills as BUY-TO-OPEN** (×2); one leg still `order_must_omit_price` | idempotency/replay + incomplete fix | — |
| 19:34 MDT | commit `b3347ba` **reverts** the whole fix → back to failure mode #1 | — | — |

\* 15 exit-eval failures total across the day (3+4+6+2); the earliest few also appear in the
overnight log `meic-20260719T170124Z.log`.

## 4. Impact (observed in logs)

- **No automated exit fired all session.** TPF, TPT, manual Close, and buyback escalation were all dead.
- **Manual intervention required.** ≥4 SPX stop/close orders were filled from `source: ELECTRON;0.159.1`
  (tastytrade desktop) — the operator hand-managing the book, e.g. 09:53 BTC C7505, 10:09 BTC C7505,
  11:41 stop BTC P7450.
- **Unintended position re-open (idempotency/replay).** At 11:00:56 and 11:01:58, `close:` intents for
  `SPXW 260720C07505000` submitted as **Market** and **filled as "Buy to Open"** — i.e. a close request
  opened a *new* long call. The C7505 short had already been closed manually at 10:09, so a stale/replayed
  close acted on a flat leg and re-established a position.
- **State desync → own legs quarantined (OWN-03).** After restart and at EOD the bot logged
  `ALERT[critical] FOREIGN position SPXW 260720{P07380,P07385,P07420,P07430,P07435,P07450,C07505,C07535,C07555}:
  quarantined — never stopped, closed or counted`. The bot lost ownership of legs it had opened, so its
  risk gate and P&L for the day are unreliable.

## 5. Current code state (⚠️)

The same-day fix (`8e1f36e`) was **reverted** by `b3347ba` (2026-07-20 19:34 MDT). On `main` right now:
`marketable_close()` again emits `marketable_limit` + price, and `"market"` is absent from both
`ORDER_TYPES` and the adapter map. **The defect that broke 07-20 is live again.**

## 6. Recommended fix

1. Make `marketable_close()` emit a **true market order with no price**:
   - add `"market"` to `ORDER_TYPES`, keep it **out** of `PRICED_TYPES` (`order_intent.py`);
   - map `"market" → OrderType.MARKET` in `adapters/tastytrade/adapter.py` (verify the enum name in the
     pinned SDK — source shows `tastytrade v13.1.0`);
   - drop the `price=` argument at both call sites (`close_entry.py:145`, `watchdog.py:114`).
   - (Alternative if a bare market order is undesirable for SPX: send a genuine **marketable_limit** —
     a limit crossing the spread — but tastytrade still requires the price be *aggressive*, not omitted;
     the current failure is specifically "must omit price," so a plain market order is the direct fix.)
2. **Idempotency for closes (CLS-01/ORD-04):** a close intent must be a no-op when the target leg is
   already flat. Guard against replay on restart and confirm the `idempotency_key` is honored for close
   orders so a stale/duplicated close can never fill as Buy-to-Open.
3. **Reconciliation vs OWN-03:** when the bot's own close/stop chain desyncs from the broker, it must
   re-adopt its legs rather than quarantine them as FOREIGN.
4. **Regression tests:** (a) `marketable_close()` produces a broker-valid order that the fake/sandbox
   adapter accepts; (b) a close against an already-flat leg is a no-op (never Buy-to-Open); (c) an exit
   evaluator exception never silently no-ops a due TPF/TPT — surface/alert.
5. **Verify in sandbox before live:** drive a TPF and a TPT to fire end-to-end and confirm a fill.

## 7. Open questions for the developer

- Why was `8e1f36e` reverted — did it cause a worse failure in testing, or was it just unverified?
- Does `broker.working_orders()` reliably return all resting stops so `broker.replace()` can cancel-and-close atomically?
- Is there HTTP-layer buffering that replays a close request across a restart (the observed re-open)?

---
*Report generated from the retained 2026-07-20 logs; every claim above is backed by a line in
`evidence-2026-07-20.log`.*
