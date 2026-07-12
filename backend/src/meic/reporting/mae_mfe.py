"""RPT-12 (D8/D10) MAE -- trigger-distance consumed, computed ONLY from
recorded `EntryMarkSample` values. D10: no interpolation, ever -- a missing
sample for an entry/side renders as an honest gap (`None`), never a guess.

MAE is expressed as the fraction of (trigger - fill) consumed at the WORST
recorded mark: for a short premium, a mid RISING toward the trigger is
adverse, so the worst recorded sample is the maximum recorded mid. An entry
whose worst recorded mark never reached the trigger "survived" the excursion
(it may still have been closed some other way -- taxonomy.py owns the actual
outcome; this module only measures the excursion itself).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from meic.domain.events import EntryMarkSample

_MID_FIELD = {"PUT": "put_short_mid", "CALL": "call_short_mid"}


@dataclass(frozen=True)
class Excursion:
    mae_pct: Decimal  # fraction of (trigger - fill) consumed at the worst recorded mark
    survived: bool    # the worst recorded mark never reached the trigger


def consumed_fraction(mark: Decimal, *, fill: Decimal, trigger: Decimal) -> Decimal | None:
    """Trigger-distance consumed (operator ruling 2026-07-11): (mark - fill) /
    (trigger - fill) -- the ONE shared implementation behind RPT-12's MAE
    (`excursion` below, evaluated at the worst RECORDED sample) and the
    near-trigger drill guidance (application/drills.py, evaluated at the
    CURRENT live mark). Two call sites, one formula, so they can never
    silently diverge. `None` when trigger == fill (no distance to measure
    against -- D10-style honesty, never a fabricated zero/infinity)."""
    distance = trigger - fill
    if distance == 0:
        return None
    return (mark - fill) / distance


def excursion(entry_id: str, side: str, samples: list[EntryMarkSample], *,
              fill: Decimal, trigger: Decimal) -> Excursion | None:
    """`None` when no recorded sample carries this entry/side's mid at all --
    a gap, never a fabricated value (D10)."""
    field = _MID_FIELD[side]
    values = [getattr(s, field) for s in samples
              if s.entry_id == entry_id and getattr(s, field) is not None]
    if not values:
        return None
    worst = max(values)
    consumed = consumed_fraction(worst, fill=fill, trigger=trigger)
    if consumed is None:
        return None
    return Excursion(mae_pct=consumed, survived=worst < trigger)
