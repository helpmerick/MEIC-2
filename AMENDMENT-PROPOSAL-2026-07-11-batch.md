# Spec amendment proposal — 2026-07-11 operator-ruled batch

**To:** Ash (operator / spec owner) → reviewer bot, for hardening into the spec
**From:** coding agent
**Status:** All items RATIFIED in-session by the operator on 2026-07-11 and
BUILT (commits `b613f81..1f813fd` on `live-testing-2026-07-09`, all deployed).
This document records the rulings and proposes precise rule text. `spec/` is
hash-locked; nothing below touched it.

Related proposals already pending with the reviewer: UI-24a closed-day
rollover + computed NYSE calendar (`AMENDMENT-PROPOSAL-exchange-calendar.md`),
RPT-16 backfill (`AMENDMENT-PROPOSAL-historical-backfill.md`), and the
near-trigger drill-guidance basis (ruled 2026-07-11, built in `ef9b079`:
consumed = (mark − short fill)/(trigger − short fill), warn ≥ 50%, ONE shared
implementation with RPT-12's MAE).

---

## 1. Live stop-fill reaction chain (push + fallback) — proposed STP-08 / EC-STP-06a

**Ruling (operator, 2026-07-11):** "The stop being hit triggers the long sale
immediately; only if that fails does the periodic check force it. Once the
long is confirmed closed it never keeps trying to sell."

**Built** (`c9fe6c8`, `1f813fd`): the broker's account order-event stream
(tastytrade AlertStreamer — adapter method was contract-tested but consumed by
nothing; the 8th "exists-but-unwired" instance, closed with a fail-first
capstone) now drives the reaction; a dedicated poll is the fallback.

**Proposed rule text:**

> **STP-08 — Live stop-fill reaction.**
> 1. The live app MUST consume the broker's order-event stream as a
>    supervised background task. A terminal-FILLED event is a WAKE-UP only:
>    it immediately re-runs the same EC-STP-06 detection-and-recovery pass
>    the fallback runs. There is exactly ONE decision path; the stream never
>    carries its own matching or recovery logic.
> 2. A dedicated fallback poll drives the same pass every
>    `stop_fill_poll_seconds` (doc 06: default **15**, range 5–120,
>    out-of-range rejected to default). It is independent of the general
>    health tick.
> 3. Both callers funnel through one single-flight lock. The FALLBACK skips
>    its tick outright when the lock is held (a push pass or a running LEX
>    ladder is already handling it — never queue). The PUSH path waits its
>    turn (a fill event landing mid-pass must cause a re-run afterward,
>    never be dropped).
> 4. The pass is journal-terminal-aware: a side with `LongSold`/`SideClosed`/
>    `SideExpired` recorded never re-enters the candidate set — a sold long
>    is never re-sold, a handled stop never re-detected.
> 5. Stream lifecycle: reconnect with capped exponential backoff; exactly one
>    alert when the stream goes down (naming the fallback as still covering)
>    and one on recovery; the consumer must never crash the app. While the
>    stream is down, the fallback poll is the authoritative detector.

Tests: `tests/application/test_order_event_watch.py` (fill→pass-once,
non-filled no-op, reconnect + single alert pair, single-flight,
skip-if-busy vs wait-if-busy), `test_live_app.py` capstones (consumer wired
for real; poll loop proven to repeat on its own env-sourced interval —
strengthened after a naive ≥1-call assertion passed spuriously against the
old code).

Note for the same spec section: the detection pass's guards were ruled earlier
in-session (2026-07-10/11, F1+detection package reviews) and are already
pinned by tests — double-ladder guard (never start a second LEX ladder while
a sell-to-close rests), OWN standdown (long gone at broker → record honestly,
no order, one info alert), quote deferral (never guess a price; one critical
alert after 5 consecutive quote-less ticks), ORD-08a raced-close sides stay
pending with decay exempted side-level only (DCY-03). The reviewer may want
these codified adjacent to STP-08.

## 2. `stop_rebate_markup` UI exposure — proposed UI-18a

**Ruling:** the operator asked where to set the $0.30 long-recovery buffer;
no UI existed (dial was API-only). Built (`c9fe6c8`).

**Proposed rule text:**

> **UI-18a — Rebate-markup dial exposure.** The schedule editor MUST expose
> `stop_rebate_markup` per row ("Long-recovery buffer $"), validated
> client-side to doc 06's range/step ($0.00–$5.00, $0.05 steps) with
> reject-never-clamp semantics (backend remains authoritative). Whenever a
> row's markup is set, valid and > 0, the control displays BOTH UI-18
> disclosures before saving: the doc-03 shortfall sentence ("if the long
> recovers less than $X, your net loss exceeds {stop_loss_pct}% by the
> shortfall") AND the worst-case dollar figure `markup × 100 × contracts × 2`
> (mirrors `stop_policy.markup_worst_case_increase`; computed with exact
> decimal arithmetic, never floating point).

Tests: SchedulePanel (+9, incl. 0.30/1-lot → +$60, 0.30/2-lot → +$120,
bad-step rejected), money.ts (+12), API round-trip (+3).

## 3. Contract-dollar cash display — proposed UI-28 (new)

**Ruling:** "Show the full contract value, ×100, everywhere you display the
values" (2026-07-11); extended same day to RPT-07 stop-out slippage ("show
$10 not .10"). Built (`c549dde`, `cb379e4`).

**Proposed rule text:**

> **UI-28 — Cash amounts display as contract dollars.**
> 1. Every money AMOUNT on the trading panel (entry P&L, entry credit,
>    per-side premiums, day-report credit/P&L tiles and per-entry rows,
>    manual-fire simulated net credit) displays as real cash:
>    `premium × 100 × contracts`, formatted `+$520` / `-$40`.
> 2. Per-side amounts must remain arithmetically consistent with the credit
>    they compose (e.g. $224 + $296 = $520).
> 3. Aggregates over multiple entries are computed by summing each entry's
>    own contract-dollar amount — never by applying one flat multiplier to a
>    premium-unit total (wrong when entries carry different ENT-04 contract
>    counts).
> 4. Conversion is DISPLAY-ONLY and exact: decimal-point shift on the
>    Decimal string (no IEEE-754 multiply may touch a cash figure); the
>    journal, API payloads and domain math stay in per-share premium units.
> 5. Per-share PRICES are exempt and stay per-share, because they must match
>    broker records: stop triggers, fill prices in the activity feed,
>    quote mids, floor-candidate mids, tick counts.
> 6. RPT-07 stop-out slippage renders as cash per contract (−0.10 → "-$10")
>    while EC-STP-03's journaled per-share Decimals and definitions are
>    unchanged. Surfaces that are already broker cash (Results dashboard
>    P&L, live P/L enricher, worst-case/risk estimates) are unchanged.

Tests: money.ts exact-shift suite, DayReportView mixed-contracts
aggregation, EntryCards/ManualTradeCard pins, SlippagePanels (+3 incl.
gap-through positive case).

## 4. Daily P&L calendar — proposed UI-26a / RPT-09a

**Ruling:** widen across the page; Monday–Sunday with weekends greyed as
their own state; multiple months side by side, horizontally scrollable;
hover box with wins/losses/total P&L; click opens the day drill-down.
Built (`b613f81`).

**Proposed rule text:**

> **UI-26a — Calendar heatmap layout & interaction.**
> 1. Weeks run Monday→Sunday. Saturday/Sunday render a distinct WEEKEND
>    state, separate from "no trading day", and the legend names both.
> 2. Every month spanned by the daily series renders, oldest→newest, in a
>    horizontally scrollable strip; the page itself never scrolls sideways.
> 3. A trading-day cell shows on hover/focus a styled box with: the date,
>    "W wins · L losses", and the signed total P&L (gain/loss colored). The
>    box must not be clipped by the scroll container. A broker-imported day
>    (RPT-16) shows "win/loss breakdown not applicable" — NEVER a fabricated
>    0–0, because imported history carries no entry-level outcomes.
> 4. Clicking a trading-day cell navigates to that day's existing drill-down
>    route. Weekend and no-trading cells are inert.
> 5. (RPT-09a) The daily series carries per-day `wins`/`losses` derived from
>    the SAME fold as `entry_win_rate` (one aggregation path); the CSV daily
>    table carries the same columns, blank (not 0,0) for imported-only days.

Tests: CalendarHeatmap (9 incl. Monday-first pinned to real weekdays,
weekend-vs-idle, multi-month order, hover box, aria-label preserved,
click href, inert cells), folds (+2), reports CSV (+3).

## 5. Local-time echo label — proposed UI-23a

**Ruling:** "London" (the IANA zone's city segment) misleads a Manchester
reader; label the echo "local" instead. Built (`71fdc17`).

> **UI-23a — Local echo label.** The local-time echo (UI-23) and the UI-24
> countdown label the converted time "local", not the IANA zone's city name.
> The zone itself continues to come from the browser
> (`Intl.DateTimeFormat().resolvedOptions().timeZone`); nothing geolocates
> the operator and no location data is transmitted. Conversion is
> instant-based (carries its own date), covering DST boundaries.

---

## Verification state at this batch's HEAD (`1f813fd`, deployed on :8010)

Backend **1171 passed** (13 contract deselected, RTH-gated); frontend
**200 vitest + tsc clean**; spec lock **17/17**; traceability **216 rules /
147 TCs**. Every exists-but-unwired closure above carries a fail-first
capstone in `tests/application/test_live_app.py`.
