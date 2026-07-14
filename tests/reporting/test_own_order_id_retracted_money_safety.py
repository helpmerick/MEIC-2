"""OWN-01 append-only retraction (2026-07-14): appending `OwnOrderIdRetracted`
must be METADATA-ONLY, exactly like `OwnOrderIdBackfilled`
(tests/reporting/test_own_order_id_backfilled_money_safety.py). This pins
that `OwnOrderIdRetracted` contributes EXACTLY ZERO to every money-bearing
fold: `domain.projection.fold` and `reporting.folds.core_results`/
`day_snapshot`/`daily_net` are byte-identical (Decimal equality AND `str()`)
before and after the retraction is appended.
"""
from __future__ import annotations

from decimal import Decimal as D

from meic.application.backfill_order_ids import backfill_own_order_ids
from meic.application.retract_own_order_id import retract_own_order_ids
from meic.domain.events import CondorFilled, EntryClosed
from meic.domain.projection import fold
from meic.reporting.folds import core_results, daily_net, day_snapshot

DAY = "2026-07-10"
ENTRY_ID = f"{DAY}#1"


def _legacy_bot_log_backfilled():
    events = [
        CondorFilled(entry_id=ENTRY_ID, net_credit=D("0.40"), fee=D("0")),
        EntryClosed(entry_id=ENTRY_ID, initiator="eod"),
    ]
    backfill_own_order_ids(
        events, ENTRY_ID,
        [("482621396", "entry"), ("482621556", "stop"), ("482760202", "lex")],
        at="2026-07-12T09:00:00-04:00", note="operator-authorised backfill, RPT-16")
    return events


def test_retraction_changes_no_money_fold_byte_for_byte():
    before = _legacy_bot_log_backfilled()

    before_state = fold(before)
    before_core = core_results(before)
    before_snapshot = day_snapshot(before, DAY)
    before_daily = daily_net(before)

    after = list(before)
    appended = retract_own_order_ids(
        after, ENTRY_ID, [("482760202", "operator's own out-of-band order, not the bot's")],
        at="2026-07-14T09:00:00-04:00", note="operator ruling 2026-07-14, strict OWN-01")
    assert appended == 1
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
