"""Take-profit floor math — TPF-01..09 (pure selection/validation only).

Levels are {5..90 step 5}, selectable only when >= 5 points below the current
profit percentage (TPF selector rule, TC-TPF-01). Validation is two-layer by
design (v1.6): the UI recomputes continuously, the backend re-validates with
its own mark at arm time and REJECTS (never clamps) — this module is the
shared math both layers call. Monitoring/close orchestration is application-
layer and out of Phase-3 scope.
"""
from __future__ import annotations

from decimal import Decimal

ALL_LEVELS: tuple[int, ...] = tuple(range(5, 95, 5))  # 5..90 step 5


def valid_levels(current_profit_pct: Decimal) -> tuple[int, ...]:
    """Levels arm-able right now: at least 5 points below current profit."""
    return tuple(level for level in ALL_LEVELS if Decimal(level) <= current_profit_pct - 5)


def is_armable(level: int, current_profit_pct: Decimal) -> bool:
    """Backend arm-time re-validation: reject, never clamp."""
    return level in ALL_LEVELS and Decimal(level) <= current_profit_pct - 5


def floor_amount(level: int, net_credit: Decimal) -> Decimal:
    """The floor as dollars of retained profit: level% of the entry's net credit."""
    if level not in ALL_LEVELS:
        raise ValueError(f"invalid TPF level {level}: valid set is {ALL_LEVELS}")
    return net_credit * Decimal(level) / 100


def breached(floor: Decimal, current_profit: Decimal) -> bool:
    """Bot-side monitor predicate (explicitly NOT broker-resting — TPF spec)."""
    return current_profit <= floor
