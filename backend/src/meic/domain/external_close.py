"""External-intervention classifier — OWN-09/10/11 (pure domain).

When a tracked short's stop or position changes outside the bot's own actions,
the bot must decide between four dispositions — and, crucially, when to touch
NOTHING because the operator intervened at the broker (their cleanup, their
call). Order-stream truth wins: a FILLED stop is always a normal stop-out,
never mislabeled external (OWN-09).

Guards against false positives (OWN-09): a position-feed absence only counts
as an external close if the position was seen_open, the stop is old enough
(grace), and it is confirmed on two consecutive reconciles. Absent those, the
bot waits rather than standing down on a lagging feed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SideDisposition(str, Enum):
    STOP_OUT = "STOP_OUT"                  # stop filled -> LEX (never external)
    EXTERNAL_CLOSE = "CLOSED_EXTERNAL"     # operator closed the position -> stand down, touch nothing
    USER_UNPROTECTED = "USER_UNPROTECTED"  # operator cancelled the stop, kept the position -> never re-place
    BOT_UNPROTECTED = "BOT_UNPROTECTED"    # bot-side failure -> REC-04/STP-04 auto re-place
    STILL_OPEN = "STILL_OPEN"


@dataclass(frozen=True)
class SideObservation:
    stop_filled: bool                 # order-stream truth — outranks everything
    position_present: bool            # in the broker positions feed
    stop_working: bool                # the bot's resting stop still shows working
    stop_cancelled_by_bot: bool       # the bot itself cancelled it
    seen_open: bool = True            # OWN-09 guard (a)
    grace_elapsed: bool = True        # OWN-09 guard (b)
    confirmed_two_reconciles: bool = True  # OWN-09 guard (c)


def classify_side(o: SideObservation) -> SideDisposition:
    if o.stop_filled:  # OWN-09: a real stop-out is never mislabeled
        return SideDisposition.STOP_OUT

    if not o.position_present:  # the position is gone
        # OWN-09 external close requires all guards; else wait (feed may lag)
        if o.seen_open and o.grace_elapsed and o.confirmed_two_reconciles:
            return SideDisposition.EXTERNAL_CLOSE
        return SideDisposition.STILL_OPEN

    # position still open, but its stop is gone
    if not o.stop_working:
        if o.stop_cancelled_by_bot:
            return SideDisposition.BOT_UNPROTECTED   # STP-04/REC-04 auto re-place
        return SideDisposition.USER_UNPROTECTED      # OWN-11: never re-place
    return SideDisposition.STILL_OPEN
