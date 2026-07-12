"""LEX race + late-fill + restart reconciliation — LEX-08/09, EC-LEX-03/06.

Pure decisions layered on the RecoverLong ladder. The governing principle is
LEX-08: any fill that arrives during a cancel/replace supersedes the ladder —
the position is whatever the broker says it is. These functions classify the
race outcomes and produce the corrective action; the process manager acts.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RaceResolution:
    adopt: str                       # "old" | "new" | "both" — which fill is truth
    buy_back_qty: int                # excess to buy back if a short was created
    alert: tuple[str, str] | None    # (level, code) on a double-fill


def resolve_replace_race(*, old_filled_qty: int, new_filled_qty: int,
                         intended_qty: int) -> RaceResolution:
    """LEX-08 / EC-LEX-03: a fill arriving during cancel/replace supersedes the
    ladder. If only one order filled, adopt it (abort the replace). If BOTH
    filled, the double sell created a short position ⇒ buy back the excess
    immediately at marketable limit + critical alert."""
    total = old_filled_qty + new_filled_qty
    if total <= intended_qty:
        adopt = "old" if old_filled_qty >= new_filled_qty and old_filled_qty else "new"
        return RaceResolution(adopt=adopt, buy_back_qty=0, alert=None)
    excess = total - intended_qty
    return RaceResolution(
        adopt="both", buy_back_qty=excess,
        alert=("critical", "lex_double_fill_short_position"))


def correct_pnl_for_late_fill(*, recorded_recovery: Decimal,
                              broker_recovery: Decimal) -> dict:
    """LEX-09: a fill for the long sale arrives AFTER the bot considered the
    order cancelled. Reconcile to broker truth (REC-02) and adjust the P&L
    record by the difference."""
    delta = broker_recovery - recorded_recovery
    return {"recovery": broker_recovery, "pnl_delta": delta, "corrected": delta != 0}


def resume_ladder_on_restart(*, persisted_step: int, order_key: str,
                             broker_working_keys: set[str]) -> dict:
    """EC-LEX-06 / REC-03: on restart, resume ladder timing from the persisted
    step and rediscover the working sell order by its idempotency key. If the
    key is still working at the broker, re-attach; otherwise resubmit."""
    rediscovered = order_key in broker_working_keys
    return {"resume_step": persisted_step,
            "rediscovered": rediscovered,
            "resubmit": not rediscovered}
