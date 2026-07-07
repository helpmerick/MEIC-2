"""End-of-day working-order sweep — EOD-03.

0DTE stops carry TIF=Day and die with the session, but the day MUST NOT end on
an assumption: the bot cancels every resting order for closed/expired positions
and then CONFIRMS zero working orders remain. Any order it cannot cancel is not
swept under the rug — it raises a critical alert naming that specific order, and
the sweep reports it as unresolved so the day-complete gate can react.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _order_id(order: Any) -> str:
    """Best-effort id extraction across broker order shapes (obj or dict)."""
    for attr in ("order_id", "id"):
        v = getattr(order, attr, None)
        if v is not None:
            return str(v)
    if isinstance(order, dict):
        return str(order.get("order_id") or order.get("id"))
    return str(order)


@dataclass
class SweepResult:
    cancelled: list[str] = field(default_factory=list)
    uncancellable: list[str] = field(default_factory=list)  # named in a critical alert

    @property
    def clean(self) -> bool:
        """EOD-03: the day may end only when zero working orders remain."""
        return not self.uncancellable


class EndOfDaySweep:
    def __init__(self, broker, alerts) -> None:
        self._broker = broker
        self._alerts = alerts

    async def sweep(self) -> SweepResult:
        result = SweepResult()

        # cancel every working order (resting stops for expired/closed positions)
        before = [_order_id(o) for o in await self._broker.working_orders()]
        for oid in before:
            await self._broker.cancel(oid)

        # EOD-03: CONFIRM zero remain; whatever is still working is uncancellable
        remaining = {_order_id(o) for o in await self._broker.working_orders()}
        for oid in before:
            if oid in remaining:
                result.uncancellable.append(oid)
                self._alerts.alert(
                    "critical",
                    f"EOD-03: working order {oid} could not be cancelled",
                    order_id=oid,
                )
            else:
                result.cancelled.append(oid)
        return result
