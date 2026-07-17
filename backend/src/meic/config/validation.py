"""Config validation — UI-03/04 (backend is authoritative) + NFR-06 bind/token.

Every config value is re-validated server-side regardless of the client
(UI-03): the discrete stop-pct set, the STP-02d basis gate, and the NFR-06
rule that a non-localhost bind structurally requires a token.
"""
from __future__ import annotations

from .fee_model import FeeModelRejected, validate_fee_model
from .stop_basis import StopBasisRejected, validate_stop_basis

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
    if "bind_host" in cfg:
        validate_bind(str(cfg["bind_host"]), cfg.get("api_token"))
    if "fee_model" in cfg:
        validate_fee_model(dict(cfg["fee_model"]))  # PNL-01 -- reject, never clamp
