"""Config validation — UI-03/04 (backend is authoritative) + NFR-06 bind/token.

Every config value is re-validated server-side regardless of the client
(UI-03): the discrete stop-pct set, the STP-02d basis gate, and the NFR-06
rule that a non-localhost bind structurally requires a token.
"""
from __future__ import annotations

from datetime import time

from .fee_model import FeeModelRejected, validate_fee_model
from .stop_basis import StopBasisRejected, validate_stop_basis

EOD_CLOSE_TIME_MIN = time(9, 30)
EOD_CLOSE_TIME_MAX = time(15, 59)  # doc 06 §133/38: "09:30-15:59 ET"
MIDNIGHT_16 = time(16, 0)
# UND-03/F3 (2026-07-21 final review, validation<->runtime alignment): the
# force-close scheduler CLAMPS the effective close to (session_close - 5min)
# = 15:55 on a normal 16:00 day (`force_close_scheduler._CLOSE_SAFETY_MARGIN`).
# So a configured 15:56-15:59 would be silently ignored at runtime. Refuse it
# at validation instead -- what is ACCEPTED for a mandatory-eod-close
# underlying (/ES) is exactly what will be HONORED. Cash underlyings keep the
# looser 15:59 bound (EOD-01/02 has no such runtime clamp).
MANDATORY_EOD_CLOSE_MAX = time(15, 55)  # 16:00 - the 5-min runtime close margin

STOP_PCT_SET = tuple(range(95, 305, 5))  # {95, 100, …, 300}, exactly (STP-02, UI-04)

# RSK-02 tombstone (removed v1.32, MUST NOT BE BUILT): the daily-loss feature is
# gone. Its config keys are REJECTED as unknown (spec 06 §169) so a stale config
# reviving the feature fails loudly rather than silently doing nothing.
TOMBSTONE_KEYS = frozenset({"daily_max_loss", "daily_loss_also_flatten", "risk_eval_seconds"})

# STK-10 v1.51 tombstone: `chain_atm_band_pts` is RETIRED (a fixed ATM band
# can't track the moving far-OTM dead-strike boundary — superseded by the
# TRADE-RELATIVE reachable-set gate, domain/chain.py: `reachable_strikes`).
# Same "reject, never silently ignore" pattern as RSK-02 above.
TOMBSTONE_KEYS_V151 = frozenset({"chain_atm_band_pts"})

# STP-03 v1.67 tombstone: stop_limit is RETIRED -- MUST NOT BE BUILT (the
# 07-13 week-review found `stop_order_type` pointed at no construction path
# at all, plus dead EC-STP-08 escalation code; ruling: retire, don't build).
# `stop_limit_escalation_seconds` only ever served that deleted watchdog.
# Same "reject, never silently ignore" pattern as RSK-02/STK-10 above.
TOMBSTONE_KEYS_V167 = frozenset({"stop_order_type", "stop_limit_escalation_seconds"})

# STP-03 v1.68 tombstone sweep completion: spec/06-configuration.md now marks
# `stop_limit_offset_ticks` RETIRED too -- the v1.67 sweep flagged it as a
# live (non-retired) row and deferred to the operator rather than improvising;
# the operator has since ratified retiring it (missed in the v1.67 sweep,
# agent-caught). Same "reject, never silently ignore" pattern as above.
TOMBSTONE_KEYS_V168 = frozenset({"stop_limit_offset_ticks"})

# ENT-05 v1.81 tombstone (operator-ruled, user-blocked): the per-day
# entry-COUNT cap is RETIRED -- a real user was blocked firing a legitimate
# manual entry because the cap defaulted to the scheduled-row count and
# manual fires counted against it. A count limit is not a meaningful risk
# control; the day is bounded by RSK-04 (max_day_risk, mandatory before live)
# and the Cboe daily order cap (RSK-08, 380/day, exits never blocked). Same
# "reject, never silently ignore" pattern as RSK-02/STK-10/STP-03 above.
TOMBSTONE_KEYS_V181 = frozenset({"max_entries_per_day"})

# TPF-03b v1.94 tombstone: confirmation is a DURATION (`tp_confirmation_ms`),
# never a COUNT. A count SILENTLY RE-DENOMINATES ITSELF whenever the
# evaluation cadence changes -- "2" meant two adjacent prints on the old 60 s
# health tick, i.e. two MINUTES, and would mean 500 ms at the ratified 250 ms
# cadence. **Nobody changed the parameter; the ground moved under it.**
#
# TPF-03b(ii), why this one is REJECTED rather than migrated: 2 x 250 ms is
# EXACTLY the 500 ms `tp_confirmation_ms` default, so a silent migration would
# look correct on default config and be wrong by the full cadence ratio for
# any operator who had TUNED the count (a 5, meaning five minutes, becomes
# 1.25 s). Refusing the key forces the operator to restate the intent in units
# that cannot be reinterpreted, instead of inheriting a number whose meaning
# changed underneath them. Same "reject, never silently ignore" pattern as
# RSK-02/STK-10/STP-03/ENT-05 above.
TOMBSTONE_KEYS_V194 = frozenset({"tp_confirmation_evals"})


class ConfigRejected(ValueError):
    def __init__(self, key: str, reason: str) -> None:
        self.key, self.reason = key, reason
        super().__init__(f"config {key!r} rejected: {reason}")


def validate_stop_loss_pct(pct: int) -> None:
    if pct not in STOP_PCT_SET:
        raise ConfigRejected("stop_loss_pct", "out_of_range")  # reject, never clamp


def validate_max_effective_stop_pct(pct) -> None:
    """STP-02b effective-percentage cage (v1.67): 100-150, reject-never-clamp
    (doc 06 §32). An out-of-range cap is refused outright -- the cap itself
    must never be silently coerced into range any more than the markup it
    gates may be silently reduced."""
    from decimal import Decimal
    if not (Decimal("100") <= Decimal(str(pct)) <= Decimal("150")):
        raise ConfigRejected("max_effective_stop_pct", "out_of_range")


def validate_cal_stale_after_days(days) -> None:
    """CAL-02 (doc 11/06): 7-365, default 45 -- reject-never-clamp, same
    convention as `validate_max_effective_stop_pct` above. Staleness itself
    never blocks (CAL-07); only this THRESHOLD's own range is enforced."""
    if not (7 <= int(days) <= 365):
        raise ConfigRejected("cal_stale_after_days", "out_of_range")


def validate_cal_refresh_fail_alert_days(days) -> None:
    """CAL-09 v1.77 (doc 06): 1-14, default 3 -- reject-never-clamp, same
    convention as `validate_cal_stale_after_days` above. The THRESHOLD only;
    a broken feed itself never blocks trading (CAL-07)."""
    if not (1 <= int(days) <= 14):
        raise ConfigRejected("cal_refresh_fail_alert_days", "out_of_range")


def validate_cal_auto_refresh(value) -> None:
    """CAL-09 v1.77 (doc 06): bool, default true -- the operator's opt-out
    to manual-paste-only. Anything not a real bool is rejected rather than
    silently truthy/falsy-coerced (e.g. the string "false" is truthy in
    Python -- coercing it would silently invert the operator's intent)."""
    if not isinstance(value, bool):
        raise ConfigRejected("cal_auto_refresh", "not_a_bool")


def validate_event_warning_lead_days(days) -> None:
    """CAL-11 v1.84 (doc 06): 0-5, default 3 -- reject-never-clamp, same
    convention as `validate_cal_stale_after_days` above. The warning feed
    itself never blocks (CAL-11 rule 1); only this THRESHOLD's own range is
    enforced."""
    if not (0 <= int(days) <= 5):
        raise ConfigRejected("event_warning_lead_days", "out_of_range")


def _parse_time_of_day(value) -> time:
    """Accept a real `datetime.time` or an "HH:MM" string (the shape every
    config-facing ET time dial in this codebase uses, e.g.
    `adapters/api/server.py::_decay_cutoff_time`). Raises ValueError on
    anything else -- callers convert that to the appropriate ConfigRejected."""
    if isinstance(value, time):
        return value
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"not an HH:MM time: {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    return time(hour, minute)


def validate_eod_close_time(value) -> time:
    """EOD-01/02 (doc 06 §133) / UND-03 §38: `eod_close_time` is an ET time
    in 09:30-15:59 (reject-never-clamp). Returns the parsed time so callers
    (e.g. `validate_eod_close_deadline`) can cross-check against it without
    re-parsing."""
    try:
        parsed = _parse_time_of_day(value)
    except (ValueError, TypeError) as exc:
        raise ConfigRejected("eod_close_time", "not_a_time") from exc
    if not (EOD_CLOSE_TIME_MIN <= parsed <= EOD_CLOSE_TIME_MAX):
        raise ConfigRejected("eod_close_time", "out_of_range")
    return parsed


def validate_eod_close_deadline(eod_close_time, deadline) -> None:
    """EOD-02 (doc 06 §134): `eod_close_deadline` is the marketable-fallback
    hard deadline -- strictly AFTER `eod_close_time` and strictly before
    16:00 ET (never at/past the close itself). Reject-never-clamp, same
    convention as every other range check in this module."""
    try:
        close_time = _parse_time_of_day(eod_close_time)
        parsed_deadline = _parse_time_of_day(deadline)
    except (ValueError, TypeError) as exc:
        raise ConfigRejected("eod_close_deadline", "not_a_time") from exc
    if not (close_time < parsed_deadline < MIDNIGHT_16):
        raise ConfigRejected("eod_close_deadline", "out_of_range")


def validate_underlying(name, eod_close_time=None) -> None:
    """UND-01 (v1.86, doc 06 §37/§11): the GLOBAL `underlying` default --
    profile-driven, unverified value refused. Per-row overrides are
    validated identically by `domain/schedule.py::validate_entry`; this is
    the SAME reject-never-guess check applied to the global config key.

    UND-03/F3 (v1.86 /ES Stage 2): a profile with `mandatory_eod_close`
    ("/ES") is refused UNLESS `eod_close_time` is ALSO given and resolves to
    a valid ET time AT OR BEFORE 15:55 -- /ES is never held to settlement,
    and the runtime force-close clamps to 15:55 on a normal day, so an
    accepted value must be one the runtime will actually honor (a 15:56-15:59
    value would be silently clamped otherwise). /ES is now `enabled=True`, so
    the ONLY thing that can still refuse it is a missing/out-of-range
    `eod_close_time`."""
    from meic.domain.underlying import profile_for

    profile = profile_for(str(name))
    if profile is None:
        raise ConfigRejected("underlying", "unknown_or_unverified_underlying")
    if not profile.enabled:
        raise ConfigRejected("underlying", profile.disabled_reason or "unknown_or_unverified_underlying")
    if profile.mandatory_eod_close:
        if eod_close_time is None:
            raise ConfigRejected(
                "underlying", "UND-03/F3: /ES requires an eod_close_time at or before 15:55")
        try:
            parsed = validate_eod_close_time(eod_close_time)  # 09:30-15:59 shape/range first
        except ConfigRejected as exc:
            raise ConfigRejected(
                "underlying", "UND-03/F3: /ES requires an eod_close_time at or before 15:55") from exc
        if parsed > MANDATORY_EOD_CLOSE_MAX:
            # 15:56-15:59 would be silently clamped to 15:55 at runtime --
            # refuse it so accepted == honored.
            raise ConfigRejected(
                "underlying", "UND-03/F3: /ES requires an eod_close_time at or before 15:55")


def validate_bind(bind_host: str, api_token: str | None) -> None:
    """NFR-06: config validation refuses a non-localhost bind unless a token is
    set — the panel cannot be exposed unauthenticated, structurally."""
    if bind_host not in ("127.0.0.1", "localhost", "::1") and not api_token:
        raise ConfigRejected("bind_host", "non_localhost_requires_token")


def validate_config(cfg: dict) -> None:
    """Validate a proposed config patch. Raises ConfigRejected / StopBasisRejected
    on the first problem (UI-03: reject out-of-range regardless of client)."""
    for key in cfg:
        if key in TOMBSTONE_KEYS:
            raise ConfigRejected(key, "removed_rsk02")  # RSK-02 tombstone — must not be built
        if key in TOMBSTONE_KEYS_V151:
            raise ConfigRejected(key, "removed_v151")   # STK-10 v1.51 tombstone
        if key in TOMBSTONE_KEYS_V167:
            raise ConfigRejected(key, "removed_v167_stp03")  # STP-03 v1.67 tombstone
        if key in TOMBSTONE_KEYS_V168:
            raise ConfigRejected(key, "removed_v168_stp03_sweep")  # STP-03 v1.68 sweep completion
        if key in TOMBSTONE_KEYS_V181:
            raise ConfigRejected(key, "removed_v181_ent05")    # ENT-05 v1.81 tombstone
        if key in TOMBSTONE_KEYS_V194:
            raise ConfigRejected(key, "removed_v194_tpf03b")   # TPF-03b duration-not-count
    if "stop_loss_pct" in cfg:
        validate_stop_loss_pct(int(cfg["stop_loss_pct"]))
    if "stop_basis" in cfg:
        validate_stop_basis(str(cfg["stop_basis"]))  # STP-02d gate (per_side rejected)
    if "max_effective_stop_pct" in cfg:
        validate_max_effective_stop_pct(cfg["max_effective_stop_pct"])  # STP-02b cage
    if "cal_stale_after_days" in cfg:
        validate_cal_stale_after_days(cfg["cal_stale_after_days"])  # CAL-02
    if "cal_refresh_fail_alert_days" in cfg:
        validate_cal_refresh_fail_alert_days(cfg["cal_refresh_fail_alert_days"])  # CAL-09
    if "cal_auto_refresh" in cfg:
        validate_cal_auto_refresh(cfg["cal_auto_refresh"])  # CAL-09
    if "event_warning_lead_days" in cfg:
        validate_event_warning_lead_days(cfg["event_warning_lead_days"])  # CAL-11
    if "eod_close_time" in cfg:
        validate_eod_close_time(cfg["eod_close_time"])  # EOD-01/02 / UND-03 §38
    if "eod_close_deadline" in cfg:
        # EOD-02: deadline is only meaningful relative to a close_time. A
        # patch that sets the deadline without the close_time in the SAME
        # patch is checked against the doc 06 §38 /ES default (15:55) --
        # the tightest sane reference this single-patch check can use
        # without reading the rest of the stored config (see this
        # function's docstring: reject-never-clamp per-patch, same
        # convention as `validate_underlying` below).
        validate_eod_close_deadline(cfg.get("eod_close_time", time(15, 55)), cfg["eod_close_deadline"])
    if "underlying" in cfg:
        validate_underlying(cfg["underlying"], cfg.get("eod_close_time"))  # UND-01 / UND-03 F3
    if "bind_host" in cfg:
        validate_bind(str(cfg["bind_host"]), cfg.get("api_token"))
    if "fee_model" in cfg:
        validate_fee_model(dict(cfg["fee_model"]))  # PNL-01 -- reject, never clamp
