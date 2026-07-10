"""UI-25 trust & mode stamps -- derived purely from `DayBrokerConfirmed` /
`CorrectionRecord` events already in the log (RPT-15). Both event types are
written ONLY by `application/report_reconciler.ReportReconciler`; this module
just reads them back.

A day counts as RECONCILED once RPT-15 has resolved it one way or the other:
matched (`DayBrokerConfirmed`) or corrected (`CorrectionRecord` -- a
correction already rewrites that day's numbers to broker truth via
`reporting.corrections`, and is separately surfaced to the operator via the
alert and the RPT-08 correction count, so it is not a silent pass). A day
where the broker was UNREACHABLE has neither event and counts as
unreconciled -- reconciliation retries at the next boot/tick (RPT-15), never
silently upgraded.

RPT-16 (proposed amendment): a day imported from broker history
(`ExternalFillImported`) gets a THIRD status, `broker-imported`. Its numbers
ARE broker truth by construction, but it is never labeled `broker-confirmed`
-- that label specifically means "the bot's own computation matched the
broker's", which is meaningless for a day the bot never computed anything
for in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass

from meic.domain.events import CorrectionRecord, DayBrokerConfirmed, Event, ExternalFillImported


@dataclass(frozen=True)
class TrustStamp:
    status: str  # "broker-confirmed" | "bot-computed" | "broker-imported"
    confirmed_days: int
    total_days: int
    imported_days: int = 0

    @property
    def label(self) -> str:
        """UI-25's exact wording: "N/M days broker-confirmed" when not every
        day in scope is reconciled; a bare tick when it all is; a bare
        "broker-imported" when the whole scope is imported days. A scope
        that mixes bot-computed/confirmed days with imported ones counts the
        imported days out separately (RPT-16(4)) rather than folding them
        silently into the N/M count -- that count means something different."""
        if self.status == "broker-confirmed":
            return "broker-confirmed"
        if self.status == "broker-imported":
            return "broker-imported"
        parts = [f"{self.confirmed_days}/{self.total_days} days broker-confirmed"]
        if self.imported_days:
            parts.append(f"{self.imported_days} imported day{'s' if self.imported_days != 1 else ''}")
        return " · ".join(parts)


def reconciled_days(events: list[Event]) -> set[str]:
    return {e.date for e in events if isinstance(e, (DayBrokerConfirmed, CorrectionRecord))}


def imported_days_set(events: list[Event]) -> set[str]:
    return {e.day for e in events if isinstance(e, ExternalFillImported)}


def trust_stamp(events: list[Event], days: tuple[str, ...]) -> TrustStamp:
    reconciled = reconciled_days(events)
    imported = imported_days_set(events)
    total = len(days)
    # RPT-16(4): an imported day is never broker-confirmed, even if a stray
    # DayBrokerConfirmed/CorrectionRecord exists for the same date.
    confirmed = sum(1 for d in days if d in reconciled and d not in imported)
    imported_count = sum(1 for d in days if d in imported)
    if total and imported_count == total:
        status = "broker-imported"
    elif total and confirmed == total:
        status = "broker-confirmed"
    else:
        status = "bot-computed"
    return TrustStamp(status=status, confirmed_days=confirmed, total_days=total,
                      imported_days=imported_count)
