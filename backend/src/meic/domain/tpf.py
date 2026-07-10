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


def entry_profit_pct(
    *, net_credit: Decimal, fees: Decimal, stop_fills: Decimal,
    recoveries: Decimal, open_side_costs: dict,
) -> Decimal | None:
    """TPF-01 profit% definition — THE one evaluator TPT-01 reuses verbatim
    ("Entry profit% uses the TPF-01 definition verbatim ... one evaluator").

    profit% = (realized P&L of closed sides + unrealized P&L of open sides at
    mid) / total net credit. `open_side_costs` maps each still-OPEN side to
    its current cost-to-close (short mid − long mid); an already-stopped/
    closed side contributes nothing here — its realized effect already lives
    in `stop_fills`/`recoveries` (TPF-05). All money here is PER-SHARE
    (the same scale `net_credit`/`EntryProjection.pnl` already use) — contracts
    cancel out of a percentage, so this needs no contract count.

    Returns None when `net_credit` is zero — nothing to express a % of (an
    unfilled/never-credited entry has no floor/target math to speak of).
    """
    if net_credit == 0:
        return None
    realized = net_credit - fees - stop_fills + recoveries
    open_cost = sum(open_side_costs.values(), Decimal("0"))
    profit = realized - open_cost
    return profit / net_credit * 100
