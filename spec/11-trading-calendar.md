# 11 — Trading Calendar & Event Blackouts (CAL)

**v1.71, operator-commissioned 2026-07-15.** A year-view calendar of market
events (FOMC, CPI, NFP, Fed speakers, …) on which the operator tags no-trade
days; tags override the bot's schedule. Enforcement reuses the ratified ENT-06
blackout machinery — this doc adds the data layer, the tagging model, and the UI.

## Rules

- **CAL-01 Two event tiers, honestly separated.**
  **Tier 1 — official schedules:** FOMC decision days, CPI, NFP (Employment
  Situation), PPI, PCE, GDP releases — all published in advance by the Fed/BLS/BEA
  as yearly calendars. Imported via operator-triggered, auth-gated, read-only
  fetch (or operator-pasted table); each import is evented with `imported_at`
  and `source`. **Tier 2 — Fed speakers:** no reliable official machine feed
  exists; speaker events are best-effort imports, marked tier-2 in the UI,
  display-only in trust terms. Both tiers are TAGGABLE; neither tier is ever
  silently guessed — a day with no data shows no events, never fabricated ones.

- **CAL-02 Staleness is displayed, never hidden (D10 style).** The calendar
  shows each category's `imported_at` and coverage horizon ("CPI dates loaded
  through 2026-12"). Beyond the horizon the year view renders "no data
  imported", never blank-meaning-nothing-happens. An import older than
  `cal_stale_after_days` (default 45) banners the calendar as stale.

- **CAL-03 No-trade tags (the operator's law).** The operator may tag any
  calendar day NO-TRADE, with a label (defaults to the event name, e.g. "FOMC").
  Tags are operator-authored state: evented on create/remove, persistent, and
  **added to the REC-07 durable inventory** (this amendment extends the
  inventory list per REC-07's own extension rule). Tags are day-granular in ET
  (C2: intraday windows, e.g. "no entries after 13:30 on FOMC day", are a
  future amendment — deliberately excluded now).

- **CAL-04 Standing category rules.** The operator may set "always block
  <category>" (e.g. FOMC). A standing rule auto-tags every current AND
  later-imported event of that category; auto-tags are visually distinct from
  manual tags, individually removable (removing one day does not remove the
  rule), and the rule itself is evented and REC-07-durable.

- **CAL-05 Enforcement — entries only, through the existing gate.** A NO-TRADE
  tag blocks ENTRY attempts for that ET day via the ENT-06 blackout check
  inside ENT-03: scheduled entries skip with reason `blackout:<label>` shown on
  the card and in the day report. **Everything else runs untouched** — open
  positions, stops, LEX, TPF/TPT, decay, EOD, reconcile: a blackout is Stop
  Trading for one scheduled day, never a position action (C1).

- **CAL-06 Manual fires on a tagged day — warn-and-acknowledge (C6).** The ▶
  manual fire is FRESH operator intent (ENT-09's whole rationale), so a tag
  does not hard-block it: the OK dialog renders a prominent blackout warning
  ("Today is tagged NO-TRADE: FOMC") and requires an explicit acknowledgment
  checkbox before OK enables; the acknowledgment is evented and the entry is
  report-tagged `blackout_overridden`. The operator overriding the operator's
  own rule is sovereignty, not a breach — but it is never silent.

- **CAL-07 Gate polarity — absence of tags means trade (C7).** Unlike the halt
  gate (DAT-04a), blackouts are operator-AUTHORED additions: an empty or
  unimported calendar blocks nothing (the bot traded correctly before this
  feature existed). Staleness is surfaced (CAL-02) but never blocks. The
  blackout gate input IS registered in the NFR-07 wiring registry as a
  live-signal input (the tag store), never a constant (C8).

- **CAL-08 UI (UI-30).** A separate client-side route in the existing SPA
  (UI-27 pattern): year view (12-month grid, ET), months scrollable; event
  markers by category with tier-2 visually distinct; tagged days unmistakably
  marked; click a day → its events + tag/untag control + label editor;
  category-rule toggles ("always block FOMC") in a side panel; every change
  effective immediately (no restart) and evented. The trading panel shows the
  active tag ("Today: NO-TRADE — FOMC") whenever the current ET day is tagged,
  and skip reasons render the label. Colorblind-safe per UI-26 semantics.

## Flagged decisions (reverse any)

C1 blackouts block entries only, never management. C2 day-granular now,
intraday windows later. C3 tier split: official schedules imported; Fed
speakers best-effort tier-2. C4 standing category rules auto-tag, removable
per-day. C5 tags + rules join the REC-07 inventory. C6 manual fires
warn-and-acknowledge rather than hard-block. C7 empty calendar = trade
(operator-authored additions, unlike measured signals). C8 the gate input is
NFR-07-registered. New config: `cal_stale_after_days` (7–365, default 45).
