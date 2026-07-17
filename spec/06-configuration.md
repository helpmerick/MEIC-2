# 06 — Configuration Reference

Single source of truth for every configurable parameter. The backend config schema is generated from (or validated against) this table; the React UI renders from the backend schema (UI-01, doc 05 §8). Config is versioned and immutable per version (UC-01).

**Effectivity** column: when an intraday change takes effect. `next-entry` = subsequent entries only; `immediate` = at once; `next-day` = requires day boundary.

## Strategy

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `underlying` | symbol | SPX | next-day | — |
| `entry_times` | list of ET times | **empty — composed in the UI per session; arming with zero entries is rejected** | next-entry | ENT-01/01a |
| `entry_window_seconds` | 10–600 | 120 | next-entry | ENT-02 |
| `session_warmup_lead_seconds` | 10–300 | 60 | next-entry | ENT-08 |
| `session_token_expiry_buffer_seconds` | 60–1800 | 300 | immediate | ENT-08, REC-06 |
| `contracts_per_entry` | 1–10 | 1 | next-entry | ENT-04 — per-entry since v1.44: each schedule row carries its own `contracts` (1–10); this parameter is only the pre-fill for new rows |
| ~~`max_entries_per_day`~~ | — | — | — | RETIRED v1.81 (ENT-05 tombstoned — count cap removed; day bounded by RSK-04 dollar ceiling + Cboe order cap); config loader rejects the key |
| `strike_method` | premium \| delta | premium | next-entry | STK-02 |
| `target_premium` | $0.50–$20.00 | $3.00 | next-entry | STK-02/02a — short-leg mid target; selection ceiling = target + tolerance, never exceeded; NOT net spread credit; net = short − long fluctuates with wing cost |
| `probe_up_max` | 0–5 | 3 | next-entry | STK-02 probe walk (v1.39) — max probes above target (cap = T + 0.05×n) |
| `probe_down_max` | 1–40 | 25 | next-entry | STK-02 — max probes below target; effective floor = max(T−1.25, min_short_premium) |
| `short_delta_target` | 0.03–0.30 | 0.10 | next-entry | STK-02 |
| `short_delta_max` | ≥ target, ≤ 0.35 | 0.15 | next-entry | STK-02 |
| `wing_width` | 10–200 pts, step 5 | 50 | next-entry | STK-03 |
| `max_strike_shifts` | 0–4 | 2 | next-entry | STK-09 — SHORT's shift budget (3 strikes total incl. original); all blocked ⇒ skip `strike_collision` |
| `max_long_shifts` | 0–10 | 5 | next-entry | STK-09 — LONG's solo shift budget when its target holds a short; each shift widens the spread (RSK-04 re-evaluates) |
| `chain_completeness_pct` | 50–100 | 90 | next-entry | STK-10 v1.51 — % of the entry's TRADE-RELATIVE reachable strike set (probe range + wings + shift budgets) that must be marked before selection; MUST be wired, never hardcoded |
| `min_validated_strikes` | 3–40 | 10 | next-entry | STK-10 v1.55 — baseline viability floor, per side: fewer validated reachable strikes at warm-up ⇒ alert + retry, never trade off a sliver |
| `drill_outage_seconds` | 10–300 | 60 | immediate | UC-12 v1.56 — outage-drill disconnect duration |
| `take_profit_target_pct` | {5..95 step 5} or off | off | next-entry (live-editable per entry, TPT-02) | TPT-01 — whole-entry take-profit target; distinct from the TPF floor |
| `stop_fill_poll_seconds` | 5–120 | 15 | immediate | STP-08a — fallback fill-detection poll; authoritative while the order-event stream is down |
| `max_effective_stop_pct` | 100–150 | 110 | next-entry | STP-02b v1.67 — skips `markup_exceeds_cap` when trigger ÷ credit exceeds it; reject-never-clamp |
| `cal_stale_after_days` | 7–365 | 45 | immediate | CAL-02 (doc 11) — calendar import staleness banner threshold; staleness never blocks (CAL-07) |
| `cal_auto_refresh` | bool | true | immediate | CAL-09 v1.77 — daily official-source auto-populate; off = manual paste only |
| `cal_refresh_fail_alert_days` | 1–14 | 3 | immediate | CAL-09 — consecutive fetch failures before a persistent alert |
| `event_warning_lead_days` | 0–5 | 3 | immediate | CAL-11 v1.84 — how many TRADING days before a calendar event the dismissable warning banner appears (0 = day-of only); display-only, never blocks |
| ~~`chain_atm_band_pts`~~ | — | — | — | RETIRED v1.51 (fixed band can't track the moving 0DTE dead-strike boundary); config validation REJECTS the key |
| `chain_retry_seconds` | 1–30 | 5 | next-entry | STK-10/11 — retry interval within the entry window before `incomplete_chain` skip |
| `min_short_premium` | $0.05–$20.00 | $1.00 | next-entry | STK-05 — floor on each SHORT leg's gross premium (wings not factored) |
| `min_total_credit` | $0.10–$40.00 | $2.00 | next-entry | STK-06, ORD-03 — floor on total NET condor credit (longs factored); below ⇒ abort |
| `vix_max` | 10–100 or off | off | next-entry | ENT-06 |
| `skip_dates` | date list | [] | immediate | ENT-06 |

### Per-entry overrides

`entry_times` may alternatively be given as a list of entry objects, each optionally overriding these strategy/stop parameters for that entry only — **pin-at-Save semantics (v1.47, operator-ratified): every schedule row stores CONCRETE values for all its parameters at Save time; globals are pre-fill defaults for NEW rows only and NEVER retro-apply to saved rows (extends v1.44's `contracts_per_entry` precedent to all row fields; what the row displays is exactly what trades)** — : `contracts` (v1.44), `strike_method`, `short_delta_target`, `target_premium` (premium method), `wing_width`, `min_short_premium`, `min_total_credit`, `probe_down_max` (v1.44 — the UI may display it as dollars: n × $0.05, e.g. 15 → “within $0.75 below target”; the up-cap stays `probe_up_max`, NEVER symmetric), `stop_loss_pct`, `stop_basis`, `stop_rebate_markup`, `take_profit_target_pct` (v1.58). Example:

```yaml
entries:
  - time: "10:00"
    wing_width: 30
    stop_loss_pct: 95
  - time: "11:30"
    wing_width: 30
    stop_loss_pct: 100
```

Unset fields inherit the global value. Validation rules apply per entry after inheritance.

## Stops

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `stop_loss_pct` | **{95, 100, …, 300} (5% steps, exactly)** | **95** | next-entry (UC-08 to modify existing) | STP-02, UI-04 |
| `stop_basis` | total_credit \| short_premium \| ~~per_side~~ | **total_credit** | next-entry | STP-02 — DEFAULT is Ash's outcome contract (v1.38): trigger = pct × net credit, both shorts; one-side hit ⇒ small profit, both ⇒ ≈ the premium, never more. `per_side` GATED (STP-02d, v1.43): rejected `allocation_unverified` until 5 consecutive real fills reconcile + ratified amendment |
| `stop_rebate_markup` | $0.00–$5.00, step $0.05 | $0.00 | next-entry | STP-02b — added to trigger to pre-credit expected long recovery; UI must show worst-case increase (UI-18) |
| `min_stop_distance_ticks` | 1–20 | 2 | next-entry | STP-02c — trigger must clear each short's price by this; else skip `infeasible_stop` / close post-fill |
| ~~`stop_order_type`~~ | — | — | — | RETIRED v1.67 (stop_limit tombstoned, STP-03); config loader rejects the key |
| ~~`stop_limit_offset_ticks`~~ | — | — | — | RETIRED v1.68 (missed in the v1.67 tombstone sweep — agent-caught; config loader rejects the key) |
| ~~`stop_limit_escalation_seconds`~~ | — | — | — | RETIRED v1.67 with EC-STP-08 |
| `watchdog_grace_seconds` | 3–60 | 10 | immediate | STP-03b — mark at/above trigger this long with stop unfilled ⇒ critical alert |
| `watchdog_escalate_seconds` | 5–120 | 20 | immediate | STP-03b — total from first breach; bot buys back + cancels the sleeping stop |
| `stop_retry_seconds` | 1–30 | 5 | immediate | STP-04 |
| `stop_retry_attempts` | 1–10 | 3 | immediate | STP-04 |
| `unprotected_action` | flatten_side \| flatten_condor | flatten_side | immediate | STP-04 |
| `slippage_alert_ticks` | 1–50 | 6 | immediate | EC-STP-03 |

## Net-loss estimation (informational — NLE)

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `nle_enabled` | bool | true | immediate | NLE-01 |
| `nle_haircut_pct` | 0–80 | 30 | immediate | NLE-01 |
| `nle_min_samples` | 5–200 | 25 | immediate | NLE-07 |

## Long exit (LEX)

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `lex_start_latency_ms` | 200–10000 | 2000 | immediate | LEX-01 |
| `lex_reprice_seconds` | 3–60 | 15 | immediate | LEX-03 |
| `lex_reprice_attempts` | 1–10 | 4 | immediate | LEX-03 |
| `lex_max_spread_ticks` | 2–100 | 20 | immediate | LEX-02 |
| `lex_quote_wait_seconds` | 1–30 | 5 | immediate | LEX-02 |
| `lex_fallback_timeout_seconds` | 5–120 | 30 | immediate | LEX-05/06 |
| `lex_partial_start` | bool | false | immediate | EC-STP-05 |

## Take-profit floor (TPF)

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `tp_gap_pct` | {5, 10, 15, 20} | 5 | immediate | TPF-02 — minimum gap between current profit% and a selectable floor |
| `tp_confirmation_evals` | 1–10 | 2 | immediate | TPF-03 — consecutive valid breaches required to trigger |

The floor levels themselves ({5..90 step 5}) are fixed by TPF-02, not configurable; the floor value is set per entry at runtime via the UI (UC-13), not in config.

## Decay buyback (DCY)

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `decay_buyback_enabled` | bool | true | immediate | DCY-01 |
| `decay_buyback_trigger` | $0.05–$0.50, step $0.05 | $0.05 | immediate | DCY-01 — fires when short's ask ≤ trigger |
| `decay_confirmation_evals` | 1–10 | 2 | immediate | DCY-01 |
| `decay_unfilled_timeout_seconds` | 5–120 | 30 | immediate | DCY-02 — re-inflation guard: cancel buyback, re-place stop |
| `decay_cutoff_time` | ET time | 15:55 | next-day | DCY-01 — no buybacks after this; expiry finishes the job |

## Entry orders

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `entry_reprice_seconds` | 3–120 | 20 | next-entry | ORD-02 — floor lowered 5→3 s (v1.44, operator-ratified); below 3 s rejected: an aggressive walk must not starve the API budget EC-API-02 reserves for exits |
| `entry_reprice_attempts` | 1–10 | 5 | next-entry | ORD-02 |
| `partial_fix_seconds` | 5–60 | 15 | immediate | EC-ENT-06 |
| `reject_retry_seconds` | 1–30 | 5 | immediate | EC-ENT-08 |
| `bp_reject_lockout` | bool | true | immediate | EC-ENT-07 |

## End of day

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `eod_close_time` | ET time or off | off (hold to settlement) | next-day | EOD-01/02 |
| `eod_close_deadline` | ET time > close_time | 15:59 | next-day | EOD-02 |
| `min_time_before_close` | 15–120 min | 30 | next-entry | DAY-02 |

## Risk & safety

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `trading_mode` | paper \| live | paper | next-day, flat book, typed confirm | DAY-05, UC-10 |
| `max_day_risk` | $ > 0 | required, no default | next-entry | RSK-04 |
| `min_buying_power` | 0–100,000 | 5000 | next-entry | ENT-03 — entries skip `insufficient_bp` when account derivative BP is below this floor; v1.51: previously env-only (MEIC_MIN_BUYING_POWER, a silent unspecced config — defect class as chain_completeness_pct); operator-tuned per account size (ratified live 2026-07-09: Ash runs 2,000; default stays conservative) |
| `reporting_capital_base` | $ > 0 | required for return metrics | immediate | RPT-04 (doc 10) — operator-set; account net-liq rejected (foreign capital would pollute ROC) |
| `sharpe_risk_free_pct` | 0–10 step 0.25 | 0 | immediate | RPT-04 (doc 10) |
| `report_min_sample_days` | 5–100 | 20 | immediate | RPT-04 (doc 10) — Sharpe/Sortino render 'insufficient data' below this |
| `sanity_price_multiple` | 1.5–10 | 3 | immediate | RSK-05 |
| `block_entries_on_critical` | bool | true | immediate | RSK-06 |
| `alert_channels` | list (ui, webhook, email) | [ui] | immediate | RSK-06 |
| `recovery_sla_seconds` | 10–300 | 60 | immediate | EC-RSK-05 |
| `external_close_grace_seconds` | 10–600 | 60 | immediate | OWN-09 — min age of a stop before position-feed absence can mean external close |
| `max_clock_drift_ms` | 1000–10000 | 2000 | immediate | DAY-03, RSK-07 — v1.48: measured vs broker Date header (~1 s resolution), so sub-1000 values are noise and rejected |
| `daily_order_cap` | 50–389 | 380 | immediate | RSK-08 |
| `order_cap_buffer` | 5–50 | 10 | immediate | RSK-08 |

## Paper-mode simulation (SIM)

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `sim_starting_cash` | $1,000–$10,000,000 | $100,000 | next-day | SIM-04 |
| `sim_fill_through_ticks` | 0–5 | 1 | next-entry | SIM-02 — pessimism margin beyond mid before a limit "fills" |
| `sim_stop_slippage_ticks` | 0–20 | 3 | next-entry | SIM-03 — added to trigger on simulated stop fills |
| `sim_trigger_source` | mark \| last | mark | next-day | SIM-03 — align with STP-05a's verified live trigger source |

## Data & infra

| Parameter | Type / Range | Default | Effectivity | Rules |
|---|---|---|---|---|
| `max_quote_age_ms` | 500–15000 | 3000 | immediate | DAT-02, STK-04, LEX-02 |
| `session_probe_seconds` | 15–300 | 60 | immediate | NFR-02 |
| `session_refresh_seconds` | 60–900 | 300 | immediate | NFR-02 — keep well under the actual token lifetime (verify in sandbox) |
| `http_timeout_seconds` | 2–60 | 10 | immediate | NFR-03 |
| `feed_demand_reconnect_seconds` | 1–10 | 2 | immediate | NFR-04 — bound on the skip-the-backoff reconnect attempt at decision moments |
| `bind_host` | host/IP | 127.0.0.1 | restart | NFR-06 — non-localhost requires `api_token` (validation-enforced) |
| `api_token` | secret string or unset | unset | restart | NFR-06 |
| `fee_model` | per-contract fee table | tastytrade SPX schedule (verify at build time) | next-day | PNL-01 |
| `pnl_reconcile_tolerance` | $0.01–$1.00 | $0.05 | immediate | PNL-04 — per-entry divergence above this flags PnlMismatch |

## Validation rules (backend-enforced, TC-UI-01)

1. `stop_loss_pct` must be a member of {95..300 step 5} — reject anything else, including 94, 96, 300.1. `stop_basis` must be exactly `total_credit` or `short_premium`; `per_side` is rejected with reason `allocation_unverified` while the STP-02d gate is in force (v1.43). (Pre-v1.43 text omitted `short_premium` from the valid set — corrected.)
2. `short_delta_max ≥ short_delta_target`. (`min_short_premium` and `min_total_credit` have different bases — gross short premium vs total net — so no ordering constraint links them.)
3. `entry_times` strictly increasing, all within market hours, each ≥ `min_time_before_close` before the (possibly early) close. **DAY-06 (v1.53):** each time must be 24-hour military HH:MM ET (dot separator canonicalised), and 09:30 ≤ t < 16:00 ET; violations reject per-row (`not_24h_military` / `outside_market_hours`). **Durable row ids (v1.53, ENT-10):** every saved row carries a durable entry id — all tracking keys on it, never list position.
4. `max_day_risk` is mandatory before live mode can be enabled. (`daily_max_loss` no longer exists — RSK-02 removed v1.32; the config loader REJECTS it as an unknown key.)
5. Every config save produces a new immutable version; the active version ID is stamped on every domain event (audit, UC-07).
