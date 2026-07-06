"""Reconcile — startup/reconnect reconciliation and crash recovery (REC-02..05).

Broker is authoritative for positions and fills; the event log is
authoritative for intent (REC-02). On boot the bot rebuilds intent from the
log, fetches broker truth, and produces a RecoveryPlan that re-attaches to
resting stops, resumes LEX for stopped sides, cancels stale entry orders
(ORD-06), and re-places any genuinely missing stop (REC-04). Idempotency keys
(ORD-04/REC-05) mean recovery never duplicates an order that already exists.

A short with no resting stop is triaged in REC-04 order: (1) its stop FILLED
(in the fills feed) ⇒ it was a stop-out, run LEX; (2) cancelled by someone
other than the bot ⇒ operator intent, do NOT re-place (USER_UNPROTECTED);
(3) otherwise ⇒ UNPROTECTED, re-place immediately.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from meic.domain.events import LongSaleStarted, ReconciliationMismatch, StopReplaced


@dataclass(frozen=True)
class TrackedShort:
    entry_id: str
    side: str
    symbol: str
    stop_order_id: str | None   # the stop the bot recorded placing (event log)
    stop_filled: bool           # its stop shows FILLED in the fills feed
    stop_cancelled_by_operator: bool = False  # OWN-11


@dataclass
class RecoveryPlan:
    place_stops: list[tuple[str, str]] = field(default_factory=list)      # REC-04(3)
    run_lex: list[tuple[str, str]] = field(default_factory=list)           # REC-04(1) + REC-03
    user_unprotected: list[tuple[str, str]] = field(default_factory=list)  # REC-04(2)
    cancel_orders: list[str] = field(default_factory=list)                 # ORD-06 stale
    mismatches: list[str] = field(default_factory=list)                    # REC-02 -> RSK-03


class Reconcile:
    def __init__(self, broker, events: list) -> None:
        self._broker = broker
        self._events = events

    def plan(
        self,
        *,
        tracked_shorts: list[TrackedShort],
        broker_working_order_ids: set[str],
        mid_lex_sides: list[tuple[str, str]],
        stale_entry_order_ids: list[str],
        position_mismatches: list[str] | None = None,
    ) -> RecoveryPlan:
        p = RecoveryPlan()
        for s in tracked_shorts:
            if s.stop_filled:  # REC-04(1): it stopped out — resume the long sale
                p.run_lex.append((s.entry_id, s.side))
            elif s.stop_order_id in broker_working_order_ids:
                pass  # covered — re-attach to the resting stop, no new order (REC-03)
            elif s.stop_cancelled_by_operator:  # REC-04(2): operator intent, stand down
                p.user_unprotected.append((s.entry_id, s.side))
            else:  # REC-04(3): genuinely unprotected — re-place
                p.place_stops.append((s.entry_id, s.side))
        p.run_lex.extend(mid_lex_sides)          # REC-03: resume in-flight LEX ladders
        p.cancel_orders.extend(stale_entry_order_ids)
        p.mismatches.extend(position_mismatches or [])
        return p

    async def execute(self, plan: RecoveryPlan) -> None:
        """Drive the plan against the broker. Idempotency-keyed (REC-05) so a
        recovered stop/cancel never duplicates a live order."""
        for detail in plan.mismatches:
            self._events.append(ReconciliationMismatch(detail=detail))  # RSK-03 gates trading

        for order_id in plan.cancel_orders:
            await self._broker.cancel(order_id)

        placed_keys: set[str] = set()
        for entry_id, side in plan.place_stops:
            key = f"stop:{entry_id}:{side}"
            if key in placed_keys:
                continue  # REC-05: never duplicate within one recovery pass
            placed_keys.add(key)
            await self._broker.submit({
                "action": "buy_to_close", "type": "stop_market", "tif": "Day",
                "leg": f"short_{side.lower()}", "entry_id": entry_id, "idempotency_key": key})
            self._events.append(StopReplaced(entry_id=entry_id, side=side))

        for entry_id, side in plan.run_lex:
            self._events.append(LongSaleStarted(entry_id=entry_id, side=side))
