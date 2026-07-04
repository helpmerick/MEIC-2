"""Net-loss estimation — NLE-01 (informational only, NEVER trigger math).

Chain-implied per-side net-loss estimate (doc 01 §5a): the stop trigger
enters as an INPUT parameter here; nothing in this module computes or adjusts
triggers (NLE-04's ban on model-driven trigger adjustment, and the Phase-3
stop-semantics freeze, both hold).

Steps (NLE-01):
  1. Implied move D: interpolate on the live chain (same side, same expiry)
     for the strike K* whose mid equals the stop trigger; D = |K* - short|.
  2. Raw long estimate: interpolated mid at (long strike shifted D closer to
     the money).
  3. Haircut: estimate = raw * (1 - nle_haircut_pct).
  4. Estimated net loss = (trigger - short fill) - (estimate - long fill).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping


@dataclass(frozen=True)
class NetLossEstimate:
    implied_move: Decimal
    raw_long_estimate: Decimal
    haircut_estimate: Decimal
    estimated_net_loss: Decimal


@dataclass(frozen=True)
class EstimateUnavailable:
    """NLE-03: stale chain / too few strikes to interpolate. The entry
    proceeds normally; this is informational, info-level alerts only."""

    why: str


def _interp_strike_at_mid(points: list[tuple[Decimal, Decimal]], target_mid: Decimal) -> Decimal | None:
    """Strike whose mid equals target, linear between bracketing points.
    points: (strike, mid) sorted by mid ascending."""
    for (s1, m1), (s2, m2) in zip(points, points[1:]):
        if m1 <= target_mid <= m2:
            if m2 == m1:
                return s1
            frac = (target_mid - m1) / (m2 - m1)
            return s1 + (s2 - s1) * frac
    return None


def _interp_mid_at_strike(points: list[tuple[Decimal, Decimal]], strike: Decimal) -> Decimal | None:
    """Mid at a strike, linear between bracketing strikes. points sorted by strike."""
    for (s1, m1), (s2, m2) in zip(points, points[1:]):
        if s1 <= strike <= s2:
            if s2 == s1:
                return m1
            frac = (strike - s1) / (s2 - s1)
            return m1 + (m2 - m1) * frac
    return None


def estimate_net_loss(
    *,
    chain_mids: Mapping[Decimal, Decimal],  # strike -> mid, same side & expiry
    short_strike: Decimal,
    short_fill: Decimal,
    long_strike: Decimal,
    long_fill: Decimal,
    stop_trigger: Decimal,  # INPUT — computed elsewhere, never here
    nle_haircut_pct: Decimal,  # e.g. 30 (percent)
) -> NetLossEstimate | EstimateUnavailable:
    if len(chain_mids) < 2:
        return EstimateUnavailable("too few strikes to interpolate")

    by_mid = sorted(chain_mids.items(), key=lambda kv: kv[1])
    k_star = _interp_strike_at_mid(by_mid, stop_trigger)
    if k_star is None:
        return EstimateUnavailable("trigger outside interpolable mid range")
    implied_move = abs(k_star - short_strike)

    toward_money = Decimal(1) if short_strike > long_strike else Decimal(-1)
    shifted_long = long_strike + implied_move * toward_money
    by_strike = sorted(chain_mids.items(), key=lambda kv: kv[0])
    raw = chain_mids.get(shifted_long) or _interp_mid_at_strike(by_strike, shifted_long)
    if raw is None:
        return EstimateUnavailable("shifted long strike outside chain range")

    haircut = raw * (1 - nle_haircut_pct / 100)
    net_loss = (stop_trigger - short_fill) - (haircut - long_fill)
    return NetLossEstimate(implied_move, raw, haircut, net_loss)
