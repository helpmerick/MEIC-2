"""Take-profit target math — TPT-01..07 (pure selection/validation only).

Mirrors `domain/tpf.py` exactly, with the trigger direction reversed (TPT-01,
v1.58): the floor closes when profit FALLS to it; the target closes when
profit RISES to it. Levels are {5..95 step 5}, selectable only when >= 5
points ABOVE the current profit percentage (TPT-03, operator ruling 1A:
"reject, never act" — a target at or below current profit is REJECTED,
never clamped, never treated as close-now). Entry profit% is the shared
`domain.tpf.entry_profit_pct` evaluator (TPT-01: "verbatim ... one
evaluator") — this module has no evaluator of its own.
"""
from __future__ import annotations

from decimal import Decimal

ALL_LEVELS: tuple[int, ...] = tuple(range(5, 100, 5))  # 5..95 step 5


def valid_levels(current_profit_pct: Decimal) -> tuple[int, ...]:
    """Levels arm-able right now: at least 5 points ABOVE current profit."""
    return tuple(level for level in ALL_LEVELS if Decimal(level) >= current_profit_pct + 5)


def is_armable(level: int, current_profit_pct: Decimal) -> bool:
    """Backend arm-time re-validation (TPT-03): reject, never clamp."""
    return level in ALL_LEVELS and Decimal(level) >= current_profit_pct + 5


def target_amount(level: int, net_credit: Decimal) -> Decimal:
    """The target as dollars of profit (per-share, same scale as `net_credit`):
    level% of the entry's net credit."""
    if level not in ALL_LEVELS:
        raise ValueError(f"invalid TPT level {level}: valid set is {ALL_LEVELS}")
    return net_credit * Decimal(level) / 100


def reached(target: Decimal, current_profit: Decimal) -> bool:
    """Bot-side monitor predicate (TPT-04, NEVER broker-resting)."""
    return current_profit >= target


def armed_feedback(level: int, net_credit: Decimal, *, contracts: int = 1) -> dict[str, Decimal]:
    """TPT-06 (operator-confirmed format): "Exit armed: TP X% — closes at
    debit <= $D (keep >= $P)" where D = net credit * (1 - X%) (the per-spread
    debit price) and P = net credit * X% * 100 * contracts (real dollars, the
    x100 options multiplier). Pinned vector: credit 4.00, target 60%, 1
    contract => debit <= 1.60, keep >= $240."""
    if level not in ALL_LEVELS:
        raise ValueError(f"invalid TPT level {level}: valid set is {ALL_LEVELS}")
    pct = Decimal(level) / 100
    debit = net_credit * (1 - pct)
    keep = net_credit * pct * 100 * contracts
    return {"debit": debit, "keep": keep}
