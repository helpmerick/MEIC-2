"""RPT-15 correction application -- broker truth wins, always VISIBLY.

A folded/projected value is only ever overridden by a value that arrived via
a `CorrectionRecord` event already in the log (written solely by
`application/report_reconciler.ReportReconciler`). Nothing here mutates
state or recomputes anything; a day/field with no `CorrectionRecord` renders
EXACTLY its plain projection-fold value -- the invariant TC-RPT-09 pins as
"no dashboard number ever changes without a CorrectionRecord".
"""
from __future__ import annotations

from decimal import Decimal

from meic.domain.events import CorrectionRecord, DayBrokerConfirmed, Event


def corrections_for_day(events: list[Event], day: str) -> tuple[CorrectionRecord, ...]:
    return tuple(e for e in events if isinstance(e, CorrectionRecord) and e.date == day)


def is_own_scoped(record: CorrectionRecord) -> bool:
    """RPT-15 / OWN-01 (2026-07-12 own-scoping fix): the ONE place that decides
    whether a `CorrectionRecord` carries genuine, displayable broker truth.

    `scope == "own"` means its `broker_value` was computed from ONLY the bot's
    own journaled order ids. A LEGACY record (`scope` None/absent) summed the
    WHOLE shared account -- the operator's unrelated personal trades included --
    so its `broker_value` is polluted and permanently inert for display.

    Factored out so every consumer asks the SAME question: `corrected_value`
    below (may this record override a rendered number?) and
    `broker_reconciled_days` (has RPT-15 established broker truth for this
    day?) must never drift apart on what counts as a real reconciliation."""
    return record.scope == "own"


def broker_reconciled_days(events: list[Event]) -> frozenset[str]:
    """PNL-04: the set of days whose figures are BROKER-AUTHORITATIVE -- RPT-15
    has independently established the truth for them against the broker.

    A day qualifies on EITHER signal:
      - `DayBrokerConfirmed` -- the bot's own computation matched broker truth
        exactly (nothing to correct), or
      - an own-scoped `CorrectionRecord` -- broker truth differed and the
        difference is now recorded and rendered (`corrected_value`).

    A LEGACY correction (`scope` None/absent) does NOT qualify: the report
    already refuses to display it (see `is_own_scoped`), so it establishes
    nothing -- the day still renders the bot's own unverified fold."""
    confirmed = {e.date for e in events if isinstance(e, DayBrokerConfirmed)}
    corrected = {e.date for e in events
                 if isinstance(e, CorrectionRecord) and is_own_scoped(e)}
    return frozenset(confirmed | corrected)


def corrected_value(events: list[Event], day: str, field: str, fold_value: Decimal) -> Decimal:
    """The LATEST `CorrectionRecord` for (day, field) overrides `fold_value`
    with broker truth; absent one, `fold_value` renders unchanged.

    ONLY a record with `scope == "own"` may override -- that is the only
    shape written by the OWN-01/OWN-03-scoped reconciler (broker truth
    computed from the bot's own order ids alone, see
    `application/report_reconciler.py`). A LEGACY record (`scope` None or
    absent, written before the 2026-07-12 own-scoping fix) summed the WHOLE
    shared account into "broker truth" -- the real 2026-07-10 record claims
    cash_delta -534.46 for a day the bot's own trade actually made +43.68,
    because the whole-account sum also swept in the operator's own personal
    futures trade and a second, unrelated condor. Skipping any non-"own"
    record here means such a record is permanently INERT for display: it
    still exists in the append-only log (and still shows up in
    `corrections_for_day`'s drill-down history, unchanged), but a legacy
    record can never again override a rendered number -- the day's fold
    value (the bot's own honest projection) renders instead."""
    for e in reversed(events):
        if (isinstance(e, CorrectionRecord) and e.date == day and e.field == field
                and is_own_scoped(e)):
            return Decimal(e.broker_value)
    return fold_value
