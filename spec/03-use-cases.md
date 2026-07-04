# 03 — Use Cases

Operator-facing workflows. The single actor is the **Operator** (Ash). The UI is a React/TypeScript app talking to the Python backend over REST + WebSocket (doc 05). Every use case lists preconditions, flow, and postconditions; UI-specific rules carry UI IDs referenced by tests.

---

## UC-01 Configure the strategy

**Pre:** no requirement (config changes outside market hours take effect immediately; intraday changes follow the per-rule effectivity notes below).
**Flow:** Operator opens Settings; edits any parameter from doc 06; UI validates ranges client-side AND backend re-validates (single source of truth: the backend schema); operator saves; backend persists a new immutable config version with timestamp and diff.
**Post:** new config version active; audit log records who/when/what changed.

- **UI-01** Every parameter shows its current value, default, allowed range/step, and a plain-English description sourced from doc 06.
- **UI-02** Intraday effectivity is displayed per parameter (e.g. "applies to next entry", "applies immediately").
- **UI-03** Invalid values cannot be saved; the backend rejects out-of-range values regardless of UI state.
- **UI-04** `stop_loss_pct` is a discrete selector: 95%–300% in 5% steps exactly (STP-02). No free-text entry.

## UC-02 Start the trading day

**Pre:** backend running; broker session valid.
**Flow:** On boot (including Docker recovery) the bot **restores its persisted armed state** — it does not reset to DISARMED. To set up: the operator composes the standing entry schedule in the UI (count, times, per-entry premium/width/stop parameters) → presses **Arm** → backend validates (≥ 1 entry; times legal per DAY-02/validation rules) and runs the sequence: reconcile (REC-02) → verify clock (DAY-03) → load config → subscribe market data → ARMED. UI shows a pre-flight checklist with pass/fail per item.
**Post:** state `ARMED` **persists indefinitely across days and restarts**: the standing schedule fires every trading day, each day self-initializing (calendar, reconcile, warm-up) with no operator action. Entries remain editable up to the moment each fires. **Disarm** is available at any time, stops future entries without touching positions, and equally persists until the operator re-arms.

- **UI-05** The dashboard MUST always show: mode (paper/live) with unmistakable visual distinction, the three trade-enabling states (ARMED/DISARMED, Stop Trading, **Confirm Live**) each with its own clearly labelled indicator, day P&L, per-entry cards, connection health (broker + data), and active config version.

## UC-03 Watch entries execute (happy path)

**Flow:** At each `entry_times` slot the UI entry card animates through: `CHECKING → PRICING → WORKING → OPEN (stops confirmed)` or a terminal `SKIPPED(reason)`. Operator can expand a card to see legs, strikes, deltas, credits, stop trigger prices, and the live order.
**Post:** filled entries show both resting stops with broker order IDs.

- **UI-06** A skipped entry always displays its machine-readable reason (doc 02 reason codes) in human words.
- **UI-18** The `stop_rebate_markup` selector ($0.00–$5.00, $0.05 steps) sits beside the stop settings and, whenever markup > 0, displays the worst-case consequence before saving: "If the long recovers less than ${markup}, your net loss exceeds {stop_loss_pct}% by the shortfall." The NLE estimate line (UI-13) reflects the markup-inclusive trigger.
- **UI-19** FOREIGN positions render in a dedicated, visually distinct "Not managed by bot" section — never inside entry cards. The shared-symbol warning (OWN-05) and ledger-shortfall acknowledgment (OWN-06) are persistent banners; the shortfall banner carries the resume-automation action. Both banners state: "Full isolation requires not trading the bot's strikes manually."
- **UI-14** Everywhere premiums appear (entry cards, previews, reports), "short premium" and "net credit" are separate, labelled figures — never a single ambiguous "credit" number (STK-02a). Entry cards show: short premium per side, long cost per side, net per side, total net.
- **UI-13** Entry cards and the `stop_loss_pct` selector display the per-side chain-implied net-loss estimate (NLE-05): "nominal {pct}% ≈ est. net {y}% (put) / {z}% (call)", with a live preview in Settings while the market is open. Estimates are clearly labelled as estimates and shown as UNAVAILABLE when data is stale.
- **UI-07** An `UNPROTECTED` state (STP-04) renders as a full-width critical banner, not just a card badge.

## UC-04 Watch a stop-out and long recovery

**Flow:** Short stop fills → card flips to `SIDE_STOPPED` → LEX ladder progress shown live (current limit, ticks walked, time to next reprice) → `SIDE_CLOSED` with recovery amount.
**Post:** per-side P&L updated; slippage vs trigger displayed.

- **UI-08** The LEX ladder view shows each reprice as a step with timestamp and price, live.

## UC-05 Activate Stop Trading

**Pre:** any time, any state.
**Flow:** Operator presses **Stop Trading** → confirm dialog states plainly: "new entries stop; existing positions, stops and management continue" → backend executes RSK-01 → UI reflects the state globally.
**Post:** persisted (EC-RSK-03); resuming requires a deliberate second action ("Resume trading") with confirmation.

- **UI-09** Stop Trading is reachable from every screen in ≤ 2 clicks and never disabled by UI errors (it degrades to a direct API call; document the curl command in the UI help).

## UC-06 Adjust stop percentage intraday

**Flow:** Operator changes `stop_loss_pct` in Settings → UI states "applies to subsequent entries only" (STP-02/EC-STP-07) → save.
**Post:** next entries use the new pct; existing stops untouched.

## UC-07 Review the day / history

**Flow:** Operator opens Reports; selects a day; sees EOD-05 report: per-entry table (credits, stops, recoveries, fees, net), equity curve, every skip/abort with reason, alert history, config version(s) in force.
**Post:** exportable (CSV/JSON).

- **UI-10** Historical reports are immutable snapshots; regenerating from the event log must produce identical numbers (event-sourcing invariant, REC-01).

## UC-13 Set a take-profit floor on a winning entry

**Pre:** entry has open sides and positive profit ≥ (`tp_gap_pct` + 5)% of its net credit.
**Flow:** Operator presses "Set take profit" on the entry card → selector shows levels 5%–90% in 5% steps, with levels above (current profit% − `tp_gap_pct`) disabled and tooltipped ("too close — would trigger immediately") → operator picks a level → backend re-validates the gap and arms the floor (TPF-*).
**Post:** entry card shows the armed floor, live profit vs floor, and the bot-dependency warning; floor changes/clears/triggers appear in the day report.

- **UI-15** The take-profit selector renders exactly the TPF-02 rule: every level is validated against the position's **actual live profit%** so that selectable levels always undercut it by at least `tp_gap_pct` (5 points). Enabled/disabled states are **recomputed continuously from the live profit stream while the selector is open** — a level that becomes too close greys out in place. Disabled levels are visible but unclickable with the reason. The UI validation is a convenience; the backend check at arm time (TPF-02) is authoritative and uses its own mark, not the client's. The armed-floor display MUST include: "Active only while the bot is running — unlike your stop-losses, this does not rest at the broker." Trigger events show which sides were closed and at what prices.

## UC-14 Close any trade with one click

**Pre:** the entry exists in any non-terminal state.
**Flow:** Every entry card shows a **"Close trade"** button. Click → **fires immediately, no confirmation dialog** (Bug Record #16: the card already displays live P&L, open sides and marks — the "preview" is permanently on screen; a dialog is friction at exactly the moment it costs money). Backend invokes the canonical close procedure (CLS-01) with initiator `manual`; failures surface as a toast, never a blocking dialog. For a PENDING/WORKING entry the button reads **"Cancel entry"** and performs CLS-03 instead, also instantly.
**Post:** entry card shows close progress live (stop cancellations, ladder steps, fills) and the final P&L tagged `manual` in the day report.

- **UI-16** "Close trade" is available on every entry card without entering manual mode (UC-08 remains the escape hatch for leg-level surgery), and fires **without any confirmation dialog** — double-click and client retries are harmless (CLS idempotency, TC-CLS-03). The button and the TPF trigger MUST exercise the identical backend endpoint/service — the UI has no close logic of its own (CLS-02). If a TPF floor is armed on the entry, closing manually clears it. Flatten-all is the deliberate exception: it retains the typed FLATTEN confirmation (UC-15) because its blast radius is the whole day.

## UC-15 Flatten all trades

**Pre:** any time, any state.
**Flow:** Operator presses **"Flatten all"** (adjacent to Stop Trading) → confirmation dialog shows: count of open entries, count of working orders, estimated total proceeds/P&L at current marks, an "Also enable Stop Trading" checkbox (default unchecked), and — when the checkbox is off — a plain statement that scheduled entries will continue to fire after the flatten → operator types FLATTEN to confirm → backend executes RSK-01a (plus RSK-01 if the checkbox was ticked).
**Post:** every entry closed (or closing) via CLS with initiator `manual_flatten`; entries blocked; UI shows flattened state globally; reset is a deliberate second action.

- **UI-17** "Flatten all" is reachable from every screen in ≤ 2 clicks, sits beside Stop Trading but is visually distinct from it (Stop Trading blocks entries and closes nothing; Flatten All closes everything and blocks nothing), and degrades to a documented direct API call if the UI errors (same guarantee as UI-09).
- **UI-21** The **Confirm Live** toggle (ENT-01b) sits with the arming controls, visually distinct from Arm and Stop Trading, defaulting OFF on fresh install and persisted forever after. When any of the three trade-enabling states blocks entries, the dashboard states plainly which one(s): "Not trading: Confirm Live is off." No entry can ever fire with it off.
- **UI-20** A third button, **"Stop Trading & Flatten"** (RSK-01b), sits with the other two and executes both paths in one action (Stop Trading first, then Flatten All), behind the same typed-FLATTEN confirmation. The three global controls are exactly: **Stop Trading** (block entries), **Flatten all** (close everything), **Stop Trading & Flatten** (both — the full emergency stop); plus per-entry **Close trade** (UC-14). All four degrade to documented direct API calls.

## UC-08 Manually modify or close a position (escape hatch)

**Pre:** operator explicitly enters "Manual mode" for a specific entry (confirmation required).
**Flow:** Operator may: replace a resting stop at a new trigger (cancel/replace with UNPROTECTED handling per EC-STP-07), close a side at marketable limit, or close the whole condor.
**Post:** manual actions are tagged `manual` in the event log and day report; the bot resumes automated management of whatever remains only after the operator exits manual mode for that entry.

- **UI-11** Manual actions show a preview (order, price basis, estimated P&L impact) before submission.

## UC-09 Recover from a crash mid-day

**Pre:** backend restarted with open positions.
**Flow:** automatic — REC-02/03/04 run; UI shows a "Recovery" screen listing each rediscovered position, its protection state, and actions taken, until the operator acknowledges.
**Post:** EC-RSK-05 SLA met; acknowledgement recorded.

## UC-10 Run in paper mode / promote to live

**Pre:** paper mode active (fresh-install default).
**Flow:** All use cases above work identically in paper mode against the **fill simulator** (SIM-01→06): real production market data, simulated trade-through fills, simulated cash/margin ledger — NOT tastytrade's cert sandbox, whose instant fills would make paper results meaningless. Promotion to live: operator flips `trading_mode` (DAY-05: takes effect next day, requires flat book), UI requires typing the word LIVE to confirm.
**Post:** mode change audited.

- **UI-12** Paper mode shows a persistent, unmissable "PAPER" watermark; live mode a distinct "LIVE" badge.

## UC-11 Receive and acknowledge alerts

**Flow:** Critical alerts (RSK-06) appear as banner + are pushed to `alert_channels`; operator acknowledges; unacknowledged critical alerts block new entries if configured.
**Post:** acknowledgements are logged with timestamp.

## UC-12 Verify stop independence (operational drill)

A deliberate, supported procedure — not an edge case — because it validates the core design claim (STP-05):
**Flow:** In paper mode with an open condor, operator presses "Simulate bot outage" → backend disconnects its own broker/data sessions for N seconds → UI (on reconnect) shows evidence that the stop orders remained working at the broker throughout (order timestamps unbroken).
**Post:** drill result stored in reports. Recommended before first live day and after any tastytrade API change.
