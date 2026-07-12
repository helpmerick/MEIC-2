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

from .execute_entry import _fill_matches  # reused normalizer (2026-07-11 sweep), never a new one


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
    # REPRICE-RACE SWEEP (2026-07-11): an order that disappeared from
    # working_orders() not because the cancel worked, but because it FILLED
    # while the sweep was cancelling it. Named in its OWN critical alert
    # (distinct from `uncancellable`) — it is neither cleanly cancelled nor
    # stuck working, it is a real fill the day-end sweep must not misreport.
    raced_fills: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """EOD-03: the day may end only when zero working orders remain."""
        return not self.uncancellable


class EndOfDaySweep:
    def __init__(self, broker, alerts, *, own_order_ids=None) -> None:
        self._broker = broker
        self._alerts = alerts
        # OWN-03 (v1.49): the sweep touches ONLY the bot's OWN working orders. A
        # foreign working order the bot did not place is ignored — never cancelled,
        # never flagged uncancellable. `None` means "all are ours" (a dedicated /
        # flat account, and every pre-v1.49 caller). On a shared account the
        # composition passes the set of order IDs the bot actually placed.
        self._own_order_ids = None if own_order_ids is None else set(own_order_ids)

    def _is_own(self, oid: str) -> bool:
        return self._own_order_ids is None or oid in self._own_order_ids

    async def sweep(self) -> SweepResult:
        result = SweepResult()

        # cancel the bot's OWN resting orders only (foreign orders are never touched)
        before = [_order_id(o) for o in await self._broker.working_orders()
                  if self._is_own(_order_id(o))]
        for oid in before:
            await self._broker.cancel(oid)

        # EOD-03: CONFIRM the bot's own orders are gone; a foreign order still
        # working is EXPECTED and ignored — the confirmation only covers ours.
        remaining = {_order_id(o) for o in await self._broker.working_orders()}
        fills = await self._broker.fills_since(None)
        for oid in before:
            if oid in remaining:
                result.uncancellable.append(oid)
                self._alerts.alert(
                    "critical",
                    f"EOD-03: working order {oid} could not be cancelled",
                    order_id=oid,
                )
            elif any(_fill_matches(f, oid) for f in fills):
                # REPRICE-RACE SWEEP (2026-07-11): gone from working_orders(),
                # but NOT because the cancel succeeded — it filled while this
                # sweep was cancelling it (e.g. a resting stop the market
                # traded through at the closing bell). Neither adapter's
                # cancel() reliably distinguishes this from a clean cancel, so
                # trusting "not remaining" alone would misreport a real
                # stop-out as a tidy cancellation — losing the ShortStopped
                # signal and, for a short's stop specifically, the LEX
                # hand-off for its orphaned long.
                result.raced_fills.append(oid)
                self._alerts.alert(
                    "critical",
                    f"EOD-03: order {oid} FILLED while being cancelled — not a clean "
                    "cancel; verify its position and orphaned long (if any) by hand",
                    order_id=oid,
                )
            else:
                result.cancelled.append(oid)
        return result
