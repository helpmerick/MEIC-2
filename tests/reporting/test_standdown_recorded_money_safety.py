"""OWN-12 (v1.67): appending `StanddownRecorded` must be METADATA-ONLY, the
same convention as `OwnOrderIdBackfilled`/`OwnOrderIdRetracted`
(tests/reporting/test_own_order_id_retracted_money_safety.py). This pins that
`StanddownRecorded` contributes EXACTLY ZERO to every money-bearing fold:
`domain.projection.fold` and `reporting.folds.core_results`/`day_snapshot`/
`daily_net` are byte-identical (Decimal equality AND `str()`) before and
after the standdown event is appended -- the out-of-band operator disposal
is the OPERATOR's P&L (strict OWN-01), permanently absent from the bot's own
ledger; this event's only job is to make that decision auditable and
visible to journal-driven detectors, never to move a number.
"""
from __future__ import annotations

from decimal import Decimal as D

from meic.domain.events import CondorFilled, EntryClosed, ShortStopped, StanddownRecorded
from meic.domain.projection import fold
from meic.reporting.folds import core_results, daily_net, day_snapshot

DAY = "2026-07-10"
ENTRY_ID = f"{DAY}#1"


def _entry_with_a_genuine_stop_out_and_standdown():
    """The real 07-10 shape: a genuine stop-out (ShortStopped), then a
    catch-up finds the orphaned long already disposed of at the broker --
    the standdown records that, and the entry finishes closed (whatever the
    EOD/other-side path recorded)."""
    return [
        CondorFilled(entry_id=ENTRY_ID, net_credit=D("3.32"), fee=D("0")),
        ShortStopped(entry_id=ENTRY_ID, side="CALL", fill=D("3.85"), slippage=D("0.05"),
                     initiator="resting_stop", fee=D("0")),
        EntryClosed(entry_id=ENTRY_ID, initiator="eod"),
    ]


def test_standdown_changes_no_money_fold_byte_for_byte():
    before = _entry_with_a_genuine_stop_out_and_standdown()

    before_state = fold(before)
    before_core = core_results(before)
    before_snapshot = day_snapshot(before, DAY)
    before_daily = daily_net(before)

    after = list(before)
    after.append(StanddownRecorded(
        entry_id=ENTRY_ID, side="CALL",
        reason="long_not_held_at_broker",
        broker_finding="broker reports no open position in SPXW  260710C07570000",
        at="2026-07-10T15:56:20+00:00"))
    assert len(after) == len(before) + 1

    after_state = fold(after)
    after_core = core_results(after)
    after_snapshot = day_snapshot(after, DAY)
    after_daily = daily_net(after)

    assert after_state == before_state
    assert str(after_state.day_pnl) == str(before_state.day_pnl)

    assert after_core == before_core
    assert str(after_core.net_pnl) == str(before_core.net_pnl)
    assert str(after_core.gross_pnl) == str(before_core.gross_pnl)
    assert str(after_core.fees) == str(before_core.fees)

    assert after_snapshot == before_snapshot
    assert str(after_snapshot.net) == str(before_snapshot.net)
    assert str(after_snapshot.fees) == str(before_snapshot.fees)

    assert after_daily == before_daily
