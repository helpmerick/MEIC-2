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

from meic.domain.events import CorrectionRecord, Event


def corrections_for_day(events: list[Event], day: str) -> tuple[CorrectionRecord, ...]:
    return tuple(e for e in events if isinstance(e, CorrectionRecord) and e.date == day)


def corrected_value(events: list[Event], day: str, field: str, fold_value: Decimal) -> Decimal:
    """The LATEST `CorrectionRecord` for (day, field) overrides `fold_value`
    with broker truth; absent one, `fold_value` renders unchanged."""
    for e in reversed(events):
        if isinstance(e, CorrectionRecord) and e.date == day and e.field == field:
            return Decimal(e.broker_value)
    return fold_value
