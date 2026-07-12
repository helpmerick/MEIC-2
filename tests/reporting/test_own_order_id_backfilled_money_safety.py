"""THE MOST IMPORTANT TEST IN THIS SLICE.

OWN-03 / RPT-16 escape hatch (2026-07-12): backfilling an entry's real
broker order ids via `OwnOrderIdBackfilled` must be METADATA-ONLY. If it
were folded by ANY money projection -- e.g. mistaken for another
`CondorFilled` -- the log would contain two condor fills for the same
entry and DOUBLE-COUNT the credit. This test pins that `OwnOrderIdBackfilled`
contributes EXACTLY ZERO to every money-bearing fold: `domain.projection.fold`
and `reporting.folds.core_results`/`day_snapshot`/`daily_net` are
byte-identical (compared both by Decimal equality AND by `str()`, which also
catches a scale drift no `==` would notice) before and after the backfill.

The bot log below mirrors the REAL 2026-07-10 incident this whole slice
exists for: pre-fix code journaled a `CondorFilled` with no broker order id
at all, folding to a (wrong, pre-reconciliation) net of $40.00 / fees $0.00.
"""
from __future__ import annotations

from decimal import Decimal as D

from meic.application.backfill_order_ids import backfill_own_order_ids
from meic.domain.events import CondorFilled, EntryClosed
from meic.domain.projection import fold
from meic.reporting.folds import core_results, daily_net, day_snapshot

DAY = "2026-07-10"
ENTRY_ID = f"{DAY}#1"


def _legacy_bot_log():
    """The REAL pre-fix journal shape: no broker_order_id anywhere."""
    return [
        CondorFilled(entry_id=ENTRY_ID, net_credit=D("0.40"), fee=D("0")),
        EntryClosed(entry_id=ENTRY_ID, initiator="eod"),
    ]


def test_backfill_changes_no_money_fold_byte_for_byte():
    before = _legacy_bot_log()

    before_state = fold(before)
    before_core = core_results(before)
    before_snapshot = day_snapshot(before, DAY)
    before_daily = daily_net(before)

    after = list(before)
    appended = backfill_own_order_ids(
        after, ENTRY_ID,
        [("482621396", "entry"), ("482621556", "stop"), ("482760202", "lex")],
        at="2026-07-12T09:00:00-04:00", note="operator-authorised backfill, RPT-16")
    assert appended == 3
    assert len(after) == len(before) + 3

    after_state = fold(after)
    after_core = core_results(after)
    after_snapshot = day_snapshot(after, DAY)
    after_daily = daily_net(after)

    # DayState (domain/projection.py) -- the deterministic replay fold --
    # must be untouched: same entries, same P&L, to the cent.
    assert after_state == before_state
    assert str(after_state.day_pnl) == str(before_state.day_pnl)

    # RPT-02 core results -- the dashboard headline numbers.
    assert after_core == before_core
    assert str(after_core.net_pnl) == str(before_core.net_pnl)
    assert str(after_core.gross_pnl) == str(before_core.gross_pnl)
    assert str(after_core.fees) == str(before_core.fees)

    # RPT-15's own bot-side snapshot (what the reconciler compares against
    # broker truth) -- must also be completely unmoved by the backfill.
    assert after_snapshot == before_snapshot
    assert str(after_snapshot.net) == str(before_snapshot.net)
    assert str(after_snapshot.fees) == str(before_snapshot.fees)

    # Per-day net (RPT-01/04 basis).
    assert after_daily == before_daily

    # Sanity: this is really the buggy pre-fix number the incident describes,
    # not some other value the test accidentally passes on.
    assert str(before_core.net_pnl) == "40.00"
    assert str(before_core.fees) == "0"
