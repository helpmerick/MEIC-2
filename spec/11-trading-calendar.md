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

- **CAL-09 Daily auto-refresh from official sources (v1.77, operator-ruled — resolves CAL-01's open source mechanism).** Tier-1 schedules AUTO-POPULATE: once per trading day (off-hours, riding the daily self-init; plus at boot if the last success is > 24 h old), the bot fetches the OFFICIAL published calendars — federalreserve.gov (FOMC), bls.gov release schedule (CPI/PPI/Employment Situation), bea.gov (GDP/PCE) — read-only, unauthenticated, those named domains ONLY. Mandatory safety rules: (1) **parse-strict, reject-don't-replace** — a fetch that fails, parses empty, or fails plausibility (dates in publishable horizon; per-category count in its expected band, e.g. FOMC 6–10/yr) is REJECTED WHOLE; existing data is never overwritten or reduced by a bad fetch; one alert. (2) **Additive with a loud diff** — new events append (CAL-04 rules auto-tag them); a previously imported date ABSENT from today's fetch is marked DISPUTED and alerted, never silently dropped, and its NO-TRADE tag stands until the operator rules. (3) **Everything evented** — source URL, timestamp, counts, diff, success or rejection. (4) **Loud staleness** — CAL-02's banner keys off last SUCCESS; `cal_refresh_fail_alert_days` (default 3) consecutive failures raise a persistent alert. (5) **Polarity unchanged (CAL-07)** — a broken feed never blocks trading; stated honestly: auto-refresh reduces but does not eliminate the operator's responsibility to know the macro calendar. Manual paste import remains the always-available fallback. (6) **NFR-07-registered** — the refresh loop is a wiring-registry component. (7) `cal_auto_refresh` (bool, default true) for manual-only operators.

- **CAL-10 Computed events — OpEx (v1.83, operator-ratified; agent-proposed defaults accepted with one addition).** A THIRD event class alongside tier-1 (fetched official) and tier-2 (best-effort): **computed events**, derived by deterministic calendar math — no fetch, no source domain, no staleness concept (computed events are always current for any displayed year; CAL-02 banners do not apply to them). Categories: **OPEX_MONTHLY** = the monthly options expiration (third Friday of each month) and **QUAD_WITCH** = the quarterly quadruple witching (third Friday of March/June/September/December), marked visually distinct from monthly OpEx. Weekly/daily expirations are deliberately EXCLUDED — SPX trades 0DTE every day, so only the elevated-activity dates carry signal. **Holiday shift (adviser addition):** when the computed third Friday is an exchange holiday per the DAY-01a calendar (e.g., Good Friday — real vector: April 2000, the 21st), the OpEx event lands on the PRECEDING TRADING DAY — the computation consults the computed NYSE calendar, never assumes Friday. Computed events are TAGGABLE and standing-rule capable exactly like fetched ones (CAL-03/04 — "always block QUAD_WITCH" is expressible) but are NEVER auto-blocked (OpEx is not inherently no-trade the way the operator treats FOMC). Badge: a distinct UI-26 colorblind-safe marker, visually separate from economic releases. Pure function, evaluated at render/day-init — no ticking component, so no NFR-07 registry entry is required.

- **CAL-11 Event proximity warnings (v1.84, operator-commissioned; display-only, NEVER blocking).** The Trading tab shows a dismissable warning banner for any upcoming calendar event — every category (FOMC, CPI, NFP, PPI, PCE, GDP, OPEX_MONTHLY, QUAD_WITCH, and tier-2 Fed speakers) — naming the event and how close it is: on the day ("Today is FOMC") and at **T-1, T-2, T-3 TRADING days before** (counted on the DAY-01a calendar, so weekends/holidays never swallow a warning — "FOMC in 2 trading days (Wed)"). (1) **Purely informational:** it changes NO gate, blocks NO entry, and is independent of any NO-TRADE tag (a tagged day still enforces via CAL-05; an untagged event still trades — the warning is awareness, not a control). (2) **Dismissal is per-event, per-proximity-tier, and persisted (REC-07):** dismissing the T-3 banner silences only that tier for that event — the T-2, T-1, and day-of banners still appear as the event approaches (the nearest warning is the most important and is never pre-dismissed by an earlier click); a given (event, tier) dismissal never re-nags. (3) **Trust honesty:** a tier-2 (Fed speaker) warning is labeled best-effort, never stated as certain (CAL-01); a warning only ever appears for an event actually on the calendar — never fabricated. (4) **Multiple events stack** (each its own dismissable line), nearest-first. (5) Config `event_warning_lead_days` (0–5, default 3; 0 disables the pre-event tiers, leaving only day-of). Colorblind-safe per UI-26; the banner is the UI-31 tooltip standard for any jargon. The warning feed is NFR-07-registered as a UI data source (derived from the tag/event store, never a constant).

## Flagged decisions (reverse any)

C1 blackouts block entries only, never management. C2 day-granular now,
intraday windows later. C3 tier split: official schedules imported; Fed
speakers best-effort tier-2. C4 standing category rules auto-tag, removable
per-day. C5 tags + rules join the REC-07 inventory. C6 manual fires
warn-and-acknowledge rather than hard-block. C7 empty calendar = trade
(operator-authored additions, unlike measured signals). C8 the gate input is
NFR-07-registered. New config: `cal_stale_after_days` (7–365, default 45); v1.77 adds `cal_auto_refresh` (default true) and `cal_refresh_fail_alert_days` (1–14, default 3). v1.84 adds `event_warning_lead_days` (0–5, default 3).
