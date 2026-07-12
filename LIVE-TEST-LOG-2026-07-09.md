# Live-test audit log — 2026-07-09

First live testing of the MEIC bot against the **production** tastytrade account
(single-account mode, alongside the operator's ~21 pre-existing positions per spec
v1.49). Supervised manual fires via the panel `▶`. This log records every failure,
its root cause, and the fix, for audit.

**Net result:** one real 1-contract 20-wide SPXW 0DTE iron condor was **placed and
then closed** (see §"The incident"). No stops were ever placed on it (bug); it was
closed manually within ~minutes. All bugs found are fixed with regression tests.

---

## Failures & fixes (in order encountered)

| # | Symptom | Root cause | Fix (commit) |
|---|---------|-----------|--------------|
| 1 | Panel showed only auto-looping "paper" trades | Running `paper_app` (self-driving demo) not `live_app` | Launched `live_app` (real broker, DISARMED, no demo loop) |
| 2 | `row — unparsable (not enough values to unpack)` on Save | Stale server process from session start (uvicorn has no hot-reload) | Restart; separately made the parser accept a dot separator (`5a849e6`) |
| 3 | Arm blocked: `clock NOT verified (DAY-03)` | `live_app` exposed the session/clock probe but **never ran it on a timer** — the "health loop" the pre-flight assumes didn't exist. AND `_probe_response` used a non-existent `async_client`/GET; SDK v13 is `_client` + POST `/sessions/validate`, so `server_time()` always returned None | Added the periodic health loop (+ immediate probe on connect); fixed the probe attribute/method (`6c34e78`) |
| 4 | Arm blocked: `market_data stale (DAT-02)` | Same missing health loop — nothing refreshed the chain snapshot | Health loop now also refreshes the snapshot (`6c34e78`) |
| 5 | Fire skipped: `incomplete_chain` | STK-10 completeness inspected a ±120 band and required 90% marked, but far-OTM 0DTE **calls (81–116 pts OTM) are listed yet unquoted** — dead strikes failed the gate. `chain_atm_band_pts` (spec config) was **defined but never wired** (hardcoded 120) | Wired `MEIC_CHAIN_ATM_BAND_PTS` (spec default 150); set to **70** in `.env` (covers the trade's reach, inside the ~80pt quote boundary). Verified PUT 100% / CALL 100% (`e418404`) |
| 5b | `incomplete_chain` **recurred** later (spot 7530→7545) | The far-OTM call quote boundary is **not fixed** — it moves with spot/vol. By 13:38 ET, calls 7600–7615 (55–70 pts OTM) had gone unquoted, so band 70 failed again. A **fixed** band cannot track a moving illiquidity boundary | Lowered `MEIC_CHAIN_ATM_BAND_PTS` to **50** (spec minimum); PUT 100% / CALL 100% at that moment. **This is a band-aid, not a fix — see outstanding item 5.** |
| 6 | Fire skipped: `insufficient_bp` | Account derivative BP was **$3,448**, below the `MEIC_MIN_BUYING_POWER` **$5,000** floor. The trade itself only needed ~$1,665 | Operator lowered the floor to **$2,000** (config, `.env`) |
| 7 | Fire → **500**, no position | Manual fire called the **async** risk provider without awaiting it → a coroutine hit `dataclasses.replace()` | `manual_entry` now `await _maybe_await(self._risk)` (`7d09b86`) |
| 8 | **Condor placed, NO stops** (incident #1) | Two live-only object-shape bugs (see below) | `c250ac1` |
| 9 | **Condor placed, NO stops** (incident #2, after the #8 fixes) — 500 `margin_check_failed` | The reprice ladder's gap is **zero in live**: `execute_entry` calls `clock.wait_until(clock.now())` (a past/now deadline returns immediately), so `entry_reprice_seconds=20` is never applied. Sequence: submit condor → it fills at the broker (takes ~1–2s to register as "Filled") → `_filled` checks IMMEDIATELY, sees "Routed", returns False → 0s gap → ladder **replaces** = cancel (no-op, already filled) + submit a **second** condor → `margin_check_failed` → 500 → `_on_filled`/stops never run. Invisible in tests because the paper `SimulatedBroker` fills **synchronously** and the FakeClock makes `wait_until(now)` a no-op too | **FIXED** — the ladder now POLLS for the fill across the real reprice interval (`_await_fill`), never reprices a filled order, and stops the moment it fills. Position closed manually (order `482331956`). |

### The incident (failure #8) — a naked position

On the successful fire the broker **filled** the condor (order `482314017`:
P7505L / P7525S / C7550S / C7570L), then:

- `execute_entry._filled` called `f.get("order_id")` on each fill record. The live
  adapter's `fills_since` returns **SDK order objects** (`.id`/`.status`), not dicts
  → `AttributeError: 'PlacedOrder' object has no attribute 'get'`. This crashed
  fill-confirmation **after** the fill but **before** stop placement (STP-01), so
  **no stops** were placed and the bot never recorded the fill in its ledger.
- Because the ledger had no record, the bot's own Flatten treats the legs as
  FOREIGN and won't close them.

A second, latent bug on the same path: `protect_position._confirmed_qty` matched
working orders on `.order_id` only; live orders key on `.id`, so even with #1 fixed
a placed stop would never confirm → the condor would be sent down the UNPROTECTED
path.

**Close:** identified the exact 4 bot legs from the broker's authoritative order
record (isolated from the operator's pre-existing 0DTE legs), dry-ran a closing
order (validated: closing, frees $2,000 margin, no warnings), then submitted a
marketable limit — filled at t+2s (order `482320472`). Verified flat; operator's
pre-existing legs untouched.

**Fixes (`c250ac1`):** `_fill_matches` and `_confirmed_qty` now accept both paper
(dict/`.order_id`) and live (object/`.id`) shapes, each with a regression test.
Root testing gap: `FakeBroker.fills_since` returns dicts, so **no test exercised the
live object shape** — that is how both bugs shipped.

---

## Config changed on the account (in `.env`, not committed)

- `MEIC_CHAIN_ATM_BAND_PTS=70` → later **50** (was unset → hardcoded 120; spec default 150). Had to be retuned mid-session as spot moved — see outstanding item 5.
- `MEIC_MIN_BUYING_POWER=2000` (was 5000)

---

## Outstanding / recommended before the next live fire

1. **Auto-close on stop-placement failure.** `ProtectPosition` has no `close_entry`
   hook wired in **either** composition, so a genuine stop failure alerts critically
   (the alert reaches the live panel) but does **not** flatten. Wire the close hook so
   a fill can never be left unprotected. (High priority.)
2. **Arm should schedule upcoming entries.** Today `armed` does not start the
   wall-clock day loop, and there is no "Start day" control — scheduled entries only
   fire via manual `▶`. Operator flagged this as a design flaw.
3. **The live fill→stop path still has not run end-to-end against the real SDK.**
   Two shape bugs were found by inspection; a third is possible. Next fire should be
   supervised with live log-tailing and an instant-close ready.
4. Contract tests (`tests/contract/`, 13) exercise the real sandbox and were not run
   this session (need creds/network) — run them to cover the live order/fill/stop
   path offline against cert.
5. **STK-10 completeness is brittle against a moving illiquidity boundary (recurring
   `incomplete_chain`).** A *fixed* `chain_atm_band_pts` cannot track the far-OTM call
   quote boundary, which moves with spot/vol — the band had to be retuned 70→50
   mid-session (failures 5 and 5b). Options to make it robust, in preference order:
   (a) **trade-relative band** — judge completeness only over the strikes the condor
   can actually reach (≈ short target ± wing width), not a wide fixed ATM band;
   (b) **wire `chain_completeness_pct`** (also spec-defined, currently unwired at 90)
   so the operator can lower the bar; (c) **exclude never-quoted strikes** from the
   denominator (a listed strike with no two-sided market all snapshot long isn't a
   "hole"). (a) is the real fix and stops the recurrence. NOTE: even with the gate
   passing, the condor's **long** leg must itself be quoted — a fixed far-OTM thin
   zone can still cause `wing_unmarked` on the long, so a trade-relative view helps
   both. Reproduced live at spot 7545: calls unquoted from ~7600 (55 pts OTM).

6. **Reprice ladder has no real gap in live → duplicate-order-on-fill (incident #2).**
   `execute_entry._work_order` line ~253 does `await self._clock.wait_until(self._clock.now())`
   — passing *now*, so `SystemClock.wait_until` returns immediately and the configured
   `entry_reprice_seconds` never applies. Combined with checking `_filled` the instant
   after submit (before a live fill registers), the ladder reprices a filling order and
   submits a second condor. **Fix must:** (a) apply a real gap — `wait_until(now + entry_reprice_seconds)`;
   (b) check `_filled` AFTER the gap (or poll across it), and re-check before any reprice,
   so a filled order is never replaced; (c) ideally, treat "order acknowledged but terminal
   state unknown" conservatively — never reprice until the current order is confirmed
   NOT filled. Reprice tests must advance the FakeClock to simulate the gap.

7. **SYSTEMIC: the live async order/fill/reprice/stop path was only ever tested against a
   SYNCHRONOUS paper broker.** Three distinct live-only bugs surfaced in one session
   (object shapes ×2 in #8, zero reprice gap in #9), each capable of leaving a naked
   position. **ADDRESSED (coverage):** built a **live-shaped async fake broker**
   (`tests/harness/live_broker.py`: SDK-object shapes, fill LATENCY, reject-on-replace-
   after-fill) and an end-to-end test (`tests/application/test_live_fill_path.py`) that
   runs the real entry→fill→stops→verify path through it and asserts `PROTECTED`.
   Verified it FAILS against the pre-fix code (reproduces incident #2) and passes after.
   Still recommended: run `tests/contract/` against the cert sandbox for real-SDK coverage.

8. **Auto-close on stop-placement failure — STILL NOT WIRED.** `ProtectPosition` has no
   `close_entry` hook in either composition (outstanding item 1). With #9 fixed the happy
   path now rests stops, and the live-shaped test proves it; but a genuine stop-placement
   FAILURE still only alerts (loudly, to the panel) rather than auto-flattening. This is
   the last safeguard to wire before UNsupervised live use. The live-shaped harness now
   makes it straightforward to test.

## Operator-flagged PROCESS ERROR (agent behaviour, not a code bug) — closing a protected entry

On the first FULLY successful live fire (condor filled AND both stops rested — the
incident #8/#9 fixes working), the operator asked to close the entry. **The agent
cancelled the two protective stops FIRST, then tried to close the 4 legs — and the
close failed twice**, leaving the position NAKED (unprotected) with the short call
7550 sitting AT spot for several minutes:

1. Cancelled both resting stops (orders 482348015, 482348019) → position now naked.
2. Close dry-run failed: `invalid_price_increment` (net price not on the $0.05 grid).
3. Re-submitted at a mid-derived debit (3.75) → **Cancelled** (not marketable: the
   snapshot mids were unreliable near ATM, so the limit was well below the real ask).
4. Finally closed with an aggressive marketable limit (cap 12.00) → Filled (order
   482352202). Only THEN were the stops already gone.

**Why this is unacceptable:** removing a position's protection before its exit is
secured converts a defined, protected position into a naked ATM 0DTE position during
the exact window when a fast move (7550 was at spot) does the most damage. The order
of operations was backwards.

**Correct procedure (going forward):**
- **Close the position FIRST** with a guaranteed-fill order (aggressive marketable
  limit — a limit BUY fills at the real market ask, capped, so price it well through
  and let the market set the fill). Confirm FILLED and flat.
- **THEN cancel the now-orphaned stops** (they rest on a closed position and are
  harmless to cancel; a stop that fires on a flat position is a far smaller risk than
  a naked short).
- Never cancel protective stops while any leg they cover is still open, unless the
  closing order is already confirmed filled.

**Contributing factor:** the manual close priced off `snapshot_chain` MID marks,
which are unreliable near ATM in a fast tape (they underpriced the buy-back, so the
limit didn't cross). Manual closes should price off live NBBO / an aggressive
marketable limit, not a possibly-stale mid.

## Verification state at end of session

- Offline suite: **737 passed**, spec lock intact, traceability 191 rules / 129 TCs.
- Frontend: 75 passed, tsc clean.
