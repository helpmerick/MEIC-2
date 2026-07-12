"""RPT-15 -- EOD broker reconcile-and-correct (operator rule: zero drift).

Structurally read-only: this module accepts a narrow, duck-typed broker
FACADE (`positions` / `day_fills` / `cash_and_fees` only) -- never a
BrokerGateway, and it imports NOTHING from `meic.adapters` or
`meic.composition`, so it is structurally incapable of placing, replacing,
or cancelling an order (mirrors the guarantee TC-RPT-06 proves for
`meic.reporting`; asserted directly for this module by
tests/application/test_report_reconciler_structural.py). The composition
root is responsible for handing it a facade that only forwards those three
read calls to the real broker (see adapters/api/server.py's
`_BrokerReadFacade`) -- this module never touches `TastytradeAdapter` (or any
adapter) at all.

Match -> `DayBrokerConfirmed`. Mismatch -> one `CorrectionRecord` per
diverging field (never a silent overwrite) plus a critical alert. Broker
unreachable -> nothing is appended; the day stays bot-computed and is
retried at the next boot/tick (never auto-confirmed).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol

from meic.domain.events import CorrectionRecord, DayBrokerConfirmed
from meic.reporting.folds import day_snapshot


class ReadOnlyBrokerFacade(Protocol):
    """The ONLY broker surface RPT-15 may touch -- three read fetches. No
    submit/replace/cancel method exists on this type at all."""

    async def positions(self) -> list[Any]: ...
    async def day_fills(self, day: str) -> list[Any]: ...
    async def cash_and_fees(self, day: str) -> tuple[Decimal, Decimal]: ...


@dataclass(frozen=True)
class ReconcileOutcome:
    day: str
    status: str  # "confirmed" | "corrected" | "unreachable"
    corrections: tuple[CorrectionRecord, ...] = ()


def _diff(bot_value: str, broker_value: str) -> str:
    try:
        return str(Decimal(broker_value) - Decimal(bot_value))
    except (InvalidOperation, ValueError):
        return "n/a"  # e.g. the "flat" bool check -- no numeric diff to report


class ReportReconciler:
    """RPT-15: compare one day's bot-computed numbers against broker truth
    and append the outcome to the SAME durable event log every other service
    appends to (`events`; typically `composition.events`, a `DurableEventLog`
    -- see application/event_log.py -- so `CorrectionRecord`/
    `DayBrokerConfirmed` are journaled exactly like any other domain event)."""

    def __init__(self, *, broker: ReadOnlyBrokerFacade, events: list, alerts: Any = None,
                 now: Callable[[], str] | None = None) -> None:
        self._broker = broker
        self._events = events
        self._alerts = alerts
        self._now = now or (lambda: datetime.now().astimezone().isoformat())

    async def reconcile_day(self, day: str) -> ReconcileOutcome:
        try:
            positions = await self._broker.positions()
            fills = await self._broker.day_fills(day)
            cash_delta, fees = await self._broker.cash_and_fees(day)
        except Exception:  # noqa: BLE001 -- ANY failure is "unreachable", never a crash
            return ReconcileOutcome(day=day, status="unreachable")

        bot = day_snapshot(self._events, day)
        checks = {
            "flat": (str(bot.flat), str(len(positions) == 0)),
            "fill_count": (str(bot.fill_count), str(len(fills))),
            "cash_delta": (str(bot.net), str(Decimal(str(cash_delta)))),
            "fees": (str(bot.fees), str(Decimal(str(fees)))),
        }
        at = self._now()
        corrections: list[CorrectionRecord] = []
        for field_name, (bot_v, broker_v) in checks.items():
            if bot_v != broker_v:
                rec = CorrectionRecord(date=day, field=field_name, bot_value=bot_v,
                                       broker_value=broker_v, diff=_diff(bot_v, broker_v), at=at)
                self._events.append(rec)
                corrections.append(rec)

        if corrections:
            if self._alerts is not None:
                self._alerts.alert(
                    "critical",
                    f"RPT-15: {day} broker reconciliation found {len(corrections)} correction(s)",
                    fields=",".join(c.field for c in corrections))
            return ReconcileOutcome(day=day, status="corrected", corrections=tuple(corrections))

        self._events.append(DayBrokerConfirmed(
            date=day, at=at, checked={k: v[0] for k, v in checks.items()}))
        return ReconcileOutcome(day=day, status="confirmed")
