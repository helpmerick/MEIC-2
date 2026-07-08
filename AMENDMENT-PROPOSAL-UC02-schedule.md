# Proposed spec amendment — UC-02 schedule composition UI

**To:** Ash (operator) · **From:** coding agent · **Status:** awaiting ratification
**Nothing will be implemented until you ratify or reject each item below.**

Context: the reference screenshot of the predecessor bot's *Schedule & Parameters*
panel. Most of it maps cleanly onto the locked spec. Some of it does **not**, and
one item would **violate** an existing rule. This document separates the three.

---

## A. Already spec-compliant — buildable with NO amendment

These are the per-entry overrides doc 06 §37 already allows, plus the validation
rules already written. If you ratify nothing else, I can build this much today.

| UI column | Spec parameter | Range / set | Per-entry? |
|---|---|---|---|
| **TIME (ET)** | `entry_times[].time` | ET times | yes (it *is* the entry) |
| **TARGET $** | `target_premium` | $0.50–$20.00 | ✅ §37 |
| **WIDTH** | `wing_width` | 10–200 pts, step 5 | ✅ §37 |
| **STOP %** | `stop_loss_pct` | {95, 100, …, 300} exactly | ✅ §37 |
| **STATUS** | read model (`EntryProjection.status`) | PENDING/PROTECTED/… | n/a — already built |
| **×** (remove row) | UC-02 compose | — | n/a |
| **Mode** | `trading_mode` | paper \| live | global (DAY-05: flat book, typed LIVE, next-day) |
| **auto buy-back shorts ≤ $** | `decay_buyback_enabled` + `decay_buyback_trigger` | $0.05–$0.50, step $0.05 | global (DCY-01) |
| **long-recovery stop: fixed $** | `stop_rebate_markup` | $0.00–$5.00, step $0.05 | ✅ §37 (STP-02b) |
| **Save** | new immutable config version | — | doc 06 validation rule 5 (UC-07) |

Also per-entry per §37 but absent from your screenshot (they'd inherit globals):
`strike_method`, `short_delta_target`, `min_short_premium`, `min_total_credit`,
`stop_basis`.

### Validation rules (already in doc 06 — I'd enforce these server-side)
1. `entry_times` **strictly increasing**.
2. All times **within market hours** (09:30–16:00 ET, or the early close).
3. Each time **≥ `min_time_before_close`** (default 30 min) before the close.
4. `stop_loss_pct` ∈ {95..300 step 5} — reject 94, 96, 300.1.
5. `stop_basis` ∈ {`total_credit`, `short_premium`}; `per_side` rejected
   `allocation_unverified` (STP-02d gate still in force).
6. Arming with **zero entries is rejected** (ENT-01a) — already implemented.
7. Every save mints a **new immutable config version**.

### Two of your fields are *derived*, not new parameters

Your screenshot shows dollars; the spec models these as counts. **The dollars are
exactly what the spec's counts produce**, so I'd display dollars and store counts —
no amendment needed:

- **"price-walk … up to $0.25 off"** = `entry_reprice_attempts` (5) × one tick
  ($0.05 below a $3.00 credit) = **$0.25**. ORD-02 walks one tick per interval,
  hard-floored at `min_total_credit`.
- **"short must match target within $0.75"** = `probe_down_max` (15) × $0.05 =
  **$0.75**. (Default 25 → $1.25.)

---

## B. Requires your ratification — spec changes

### AMENDMENT 1 — `contracts_per_entry` becomes a per-entry override
**Why:** your **COUNT** column. Doc 06 line 16 makes `contracts_per_entry` a
*global* (1–100, default 1); §37's per-entry override list does **not** include it.

**Proposed text**, doc 06 §37 — add to the override list:
> …`stop_loss_pct`, `stop_basis`, `stop_rebate_markup`, **`contracts_per_entry`**.

**Consequence to accept:** entry size becomes per-entry, so RSK-04 `max_day_risk`
and the ENT-03 buying-power gate must evaluate the *per-entry* quantity. Worst-case
day risk is then Σ(per-entry worst case), not `n × global`.

☐ RATIFY ☐ REJECT

---

### AMENDMENT 2 — `entry_reprice_seconds` minimum lowered 5 → 1
**Why:** your panel shows **"price-walk entry: every 3 s"**. Doc 06 line 112 sets
the range **5–120**. Three seconds is currently **invalid** and would be rejected.

**Proposed text**, doc 06 line 112:
> \| `entry_reprice_seconds` \| ~~5~~**1**–120 \| 20 \| next-entry \| ORD-02 \|

**Consequence to accept:** a 1–4 s walk sends up to 10 replaces in ~10 s. EC-API-02
rate limiting still applies (entries are *lower* priority than exits), so an
aggressive walk can starve itself under 429s. I'd recommend **3 s as the floor**,
not 1 s. Alternatively: **reject this and keep 5 s.**

☐ RATIFY (min 1s) ☐ RATIFY (min 3s — recommended) ☐ REJECT (keep 5s)

---

### AMENDMENT 3 — a manual "fire this entry now" action (the ▶ button)
**Why:** your **▶** per-row button. **No rule in the spec authorises firing an entry
outside its scheduled window.** ENT-02 explicitly says an entry is *never executed
late*, and the whole entry cadence is time-driven.

This is the highest-risk item in the document: it is a button that opens a
real-money position on demand.

**Proposed new rule ENT-09 (Manual entry):**
> The operator may fire a composed entry immediately from the UI. A manual entry
> MUST run the **complete ENT-03 gate chain** plus the reconcile-mismatch and
> clock-drift blocks — it bypasses only the ENT-02 *scheduled window*, never a
> gate. It counts toward `max_entries_per_day` (ENT-05). It is recorded with
> initiator `manual_entry` and is stamped as such in the day report. It requires
> a typed confirmation in live mode. An entry already FILLED or SKIPPED cannot be
> re-fired (idempotent per entry).

**Consequence to accept:** the bot gains a path to open a position at an arbitrary
moment. My recommendation: ratify, but **live mode requires typed confirmation**,
and manual entries are visually distinct in the report.

☐ RATIFY as written ☐ RATIFY with changes ☐ REJECT (schedule-only)

---

## C. ⚠️ Would VIOLATE the spec — I recommend you reject

### The "long-recovery stop:" **dropdown**
Your panel shows `long-recovery stop: [ fixed $ ▾ ] [0.5]`. The dropdown implies at
least one **non-fixed** mode (e.g. "modeled", "auto", "use NLE estimate").

The advisory text beside it — *"last 1 stop(s): avg actual−assumed = −0.13, data
suggests ~$0.38"* — is **fine**: that's NLE-06 calibration, displayed.

But **automatically applying** that suggestion to the stop trigger would break two
rules you already ratified:

> **NLE-04 Structural isolation.** The estimator lives in a module with **no write
> path to order placement**. Stop triggers MUST be **byte-identical** whether NLE is
> enabled, disabled, or failing.

> **STP-02b.** …This is an **operator-set constant** — it is **NOT model-driven**;
> NLE-04's prohibition on automatic trigger adjustment stands.

**Recommendation:** the field stays a single **operator-set dollar value**
(`stop_rebate_markup`). The calibration figure is shown as *advice you may choose to
type in* — never applied automatically. The UI must also show the **worst-case
increase** before saving (UI-18), which I'd render as `markup × 100 × contracts × 2`.

☐ AGREE — keep it operator-set, advisory only
☐ OVERRIDE — you want a model-driven mode (this needs a *deliberate* amendment to
NLE-04 + STP-02b, and I'd want that written down explicitly)

---

## D. Gap I found while checking (not from your screenshot)

Doc 06 validation rule 5: *"Every config save produces a new immutable version; the
active version ID is **stamped on every domain event**."*

We persist `config_version` (REC-07 item 6) but **do not stamp it onto events**.
Building "Save" correctly means implementing that stamping. Flagging so it's a
conscious decision rather than a silent omission.

☐ Build stamping now (with Save) ☐ Defer (and log it as known debt)

---

## E. What I'd build, once ratified

1. **Domain**: `EntrySpec` (time + optional overrides) and `ScheduleSpec`, with all
   validation from §A — pure, fully unit-tested. *A bad schedule is a real-money
   hazard: an entry 30 s before the close, or a $0.10 premium target, must be
   un-arm-able.*
2. **Persistence**: schedule → `entry_schedule` (already REC-07 item 5, durable).
3. **Backend wiring** *(the substantive change)*: per-entry parameters must actually
   reach the engine. `LiveCondorSelector` currently takes **one global**
   `SelectionConfig`; it must resolve **per-entry** config at fire time. Same for
   stop params into `ProtectPosition`.
4. **API**: `GET /schedule`, `POST /schedule` (validate → version → persist),
   and `POST /entry/{n}/fire` *only if Amendment 3 is ratified*.
5. **UI**: the compose table (add/remove rows, inline validation, derived-dollar
   displays, UI-18 worst-case disclosure), plus the **UC-02 pre-flight checklist**
   (pass/fail per item) shown at Arm.

**Order:** domain validation + tests first, then wiring, then UI. Verified in the
cert sandbox before any production credentials go near it.

---

## Questions I need answered

1. **Amendments 1, 2, 3** — ratify / modify / reject each.
2. **Section C** — confirm the long-recovery stop stays operator-set (I strongly
   recommend yes).
3. **"short must match target within $0.75"** — the spec's tolerance is *asymmetric*
   (`probe_up_max` → +$0.15 default; `probe_down_max` → −$1.25 default). Your single
   figure is ambiguous. Is $0.75 the **downward** tolerance (i.e. `probe_down_max`
   = 15), with the upward cap left at default? Or symmetric ±$0.75?
4. **COUNT** — with Amendment 1, is `max_day_risk` evaluated as Σ(per-entry worst
   case)? (I believe it must be.)
5. **Section D** — stamp config version on events now, or defer?
