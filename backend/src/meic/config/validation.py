"""Config validation — UI-03/04 (backend is authoritative) + NFR-06 bind/token.

Every config value is re-validated server-side regardless of the client
(UI-03): the discrete stop-pct set, the STP-02d basis gate, and the NFR-06
rule that a non-localhost bind structurally requires a token.
"""
from __future__ import annotations

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


class ConfigRejected(ValueError):
    def __init__(self, key: str, reason: str) -> None:
        self.key, self.reason = key, reason
        super().__init__(f"config {key!r} rejected: {reason}")


def validate_stop_loss_pct(pct: int) -> None:
    if pct not in STOP_PCT_SET:
        raise ConfigRejected("stop_loss_pct", "out_of_range")  # reject, never clamp


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
    if "stop_loss_pct" in cfg:
        validate_stop_loss_pct(int(cfg["stop_loss_pct"]))
    if "stop_basis" in cfg:
        validate_stop_basis(str(cfg["stop_basis"]))  # STP-02d gate (per_side rejected)
    if "bind_host" in cfg:
        validate_bind(str(cfg["bind_host"]), cfg.get("api_token"))
