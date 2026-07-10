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
"""
from __future__ import annotations

from dataclasses import dataclass

from meic.domain.events import CorrectionRecord, DayBrokerConfirmed, Event


@dataclass(frozen=True)
class TrustStamp:
    status: str  # "broker-confirmed" | "bot-computed"
    confirmed_days: int
    total_days: int

    @property
    def label(self) -> str:
        """UI-25's exact wording: "N/M days broker-confirmed" when not every
        day in scope is reconciled; a bare tick otherwise."""
        if self.status == "broker-confirmed":
            return "broker-confirmed"
        return f"{self.confirmed_days}/{self.total_days} days broker-confirmed"


def reconciled_days(events: list[Event]) -> set[str]:
    return {e.date for e in events if isinstance(e, (DayBrokerConfirmed, CorrectionRecord))}


def trust_stamp(events: list[Event], days: tuple[str, ...]) -> TrustStamp:
    reconciled = reconciled_days(events)
    total = len(days)
    confirmed = sum(1 for d in days if d in reconciled)
    status = "broker-confirmed" if total and confirmed == total else "bot-computed"
    return TrustStamp(status=status, confirmed_days=confirmed, total_days=total)
