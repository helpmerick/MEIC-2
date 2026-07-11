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
from decimal import Decimal

from meic.domain.events import (
    LongSaleStarted,
    ReconciliationMismatch,
    ShortStopped,
    StopReplaced,
)

from .execute_entry import _fill_matches  # reused normalizer (2026-07-11 sweep), never a new one
from .order_intent import protective_stop, right_of


@dataclass(frozen=True)
class TrackedShort:
    entry_id: str
    side: str
    symbol: str
    stop_order_id: str | None   # the stop the bot recorded placing (event log)
    stop_filled: bool           # its stop shows FILLED in the fills feed
    stop_cancelled_by_operator: bool = False  # OWN-11
    stop_fill_price: Decimal | None = None     # EC-STP-06: fill from the fills feed
    # Doubles as (a) the trigger to derive slippage on EC-STP-06 synthesis, and
    # (b) the trigger REC-04(3) re-places at. The caller supplies it: recorded from
    # StopPlaced when the stop existed, or recomputed via stop_policy when the bot
    # crashed before ever placing one (EC-STP-02).
    stop_trigger: Decimal | None = None
    contracts: int = 1                         # ENT-04: size the re-placed stop to the position
    stop_resized_by_operator: bool = False     # OWN-10/11: operator intent, never overridden


@dataclass
class RecoveryPlan:
    place_stops: list[tuple[str, str]] = field(default_factory=list)      # REC-04(3)
    run_lex: list[tuple[str, str]] = field(default_factory=list)           # REC-04(1) + REC-03
    user_unprotected: list[tuple[str, str]] = field(default_factory=list)  # REC-04(2)
    cancel_orders: list[str] = field(default_factory=list)                 # ORD-06 stale
    mismatches: list[str] = field(default_factory=list)                    # REC-02 -> RSK-03
    # EC-STP-06: stops that filled while the bot was down — the missed ShortStopped
    # is synthesized on recovery so the projection/P&L reflects broker truth.
    synthesize_stopped: list[tuple[str, str, Decimal, Decimal]] = field(default_factory=list)
    # REC-04(3): the shorts behind `place_stops`, keyed (entry_id, side). A stop is
    # an order for a specific instrument at a specific trigger — recovery cannot
    # re-place one from a bare (entry_id, side) pair.
    stop_specs: dict[tuple[str, str], TrackedShort] = field(default_factory=dict)
    # STP-01 (v1.45): working stops whose quantity no longer covers their short.
    # (entry_id, side, working_qty, required_qty). Handled as UNPROTECTED (STP-04).
    quantity_mismatches: list[tuple[str, str, int, int]] = field(default_factory=list)

    @property
    def blocks_entries(self) -> bool:
        """REC-02/RSK-03: an unresolved reconciliation mismatch blocks NEW
        entries until it is cleared (EC-API-04). Protection/recovery still runs."""
        return bool(self.mismatches)


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
        working_stop_quantities: dict[str, int] | None = None,  # STP-01 (v1.45)
    ) -> RecoveryPlan:
        p = RecoveryPlan()
        for s in tracked_shorts:
            if s.stop_filled:  # REC-04(1): it stopped out — resume the long sale
                if s.stop_fill_price is not None:  # EC-STP-06: synthesize the missed fill
                    slippage = (s.stop_fill_price - s.stop_trigger
                                if s.stop_trigger is not None else Decimal("0"))
                    p.synthesize_stopped.append(
                        (s.entry_id, s.side, s.stop_fill_price, slippage))
                p.run_lex.append((s.entry_id, s.side))
            elif s.stop_order_id in broker_working_order_ids:
                # REC-03: covered — re-attach to the resting stop, no new order.
                # STP-01 (v1.45): unless it is working at the WRONG SIZE. A stop
                # smaller than the short it protects is silent nakedness, so it is
                # an UNPROTECTED condition (STP-04) — or operator intent (OWN-10) if
                # the operator resized it. Either way the bot never resizes it itself.
                working_qty = working_stop_quantities.get(s.stop_order_id) if working_stop_quantities else None
                if working_qty is not None and working_qty != s.contracts:
                    if s.stop_resized_by_operator:      # OWN-11/OWN-10: operator intent
                        p.user_unprotected.append((s.entry_id, s.side))
                    else:
                        p.quantity_mismatches.append((s.entry_id, s.side, working_qty, s.contracts))
                        p.mismatches.append(
                            f"stop {s.stop_order_id} for {s.entry_id}/{s.side} covers {working_qty} "
                            f"of {s.contracts} contracts — {s.contracts - working_qty} naked")
            elif s.stop_cancelled_by_operator:  # REC-04(2): operator intent, stand down
                p.user_unprotected.append((s.entry_id, s.side))
            else:  # REC-04(3): genuinely unprotected — re-place
                p.place_stops.append((s.entry_id, s.side))
                p.stop_specs[(s.entry_id, s.side)] = s
        p.run_lex.extend(mid_lex_sides)          # REC-03: resume in-flight LEX ladders
        p.cancel_orders.extend(stale_entry_order_ids)
        p.mismatches.extend(position_mismatches or [])
        return p

    async def execute(self, plan: RecoveryPlan) -> None:
        """Drive the plan against the broker. Idempotency-keyed (REC-05) so a
        recovered stop/cancel never duplicates a live order."""
        for detail in plan.mismatches:
            self._events.append(ReconciliationMismatch(detail=detail))  # RSK-03 gates trading

        # EC-STP-06: record the stop-outs that happened while the bot was down,
        # BEFORE resuming their LEX below (so the log reads stop-out → LEX).
        for entry_id, side, fill, slippage in plan.synthesize_stopped:
            self._events.append(ShortStopped(
                entry_id=entry_id, side=side, fill=fill, slippage=slippage,
                initiator="resting_stop"))

        for order_id in plan.cancel_orders:
            await self._broker.cancel(order_id)
            # REPRICE-RACE SWEEP (2026-07-11): a stale ENTRY order can fill in
            # the narrow window between boot's broker.positions() snapshot and
            # THIS cancel — neither adapter's cancel() reliably reports "it was
            # already filled" (SimulatedBroker: {"result": "terminal", ...};
            # TastytradeAdapter: {"result": "error", ...} for any cancel
            # failure). Trusting the cancel blindly would leave a genuinely
            # FILLED condor with no CondorFilled, no stop, no ProtectPosition —
            # invisible. This module cannot reconstruct that entry (ORD-09: it
            # does not know its strikes), so it never guesses; it surfaces the
            # race loudly instead, exactly like any other genuine
            # reconciliation mismatch (RSK-03 blocks new entries until the
            # operator resolves it by hand).
            for f in await self._broker.fills_since(None):
                if _fill_matches(f, order_id):
                    detail = (f"stale entry order {order_id} filled while boot was "
                             "cancelling it (ORD-06/RSK-03 race) — position may be "
                             "unprotected; operator must reconcile manually")
                    self._events.append(ReconciliationMismatch(detail=detail))
                    break

        placed_keys: set[str] = set()
        for entry_id, side in plan.place_stops:
            key = f"stop:{entry_id}:{side}"
            if key in placed_keys:
                continue  # REC-05: never duplicate within one recovery pass
            placed_keys.add(key)

            # The trigger comes from the caller on TrackedShort: either the one the
            # bot recorded placing (REC-02: the log is authoritative for intent), or
            # — for EC-STP-02, a crash BEFORE placement, where no StopPlaced exists —
            # one recomputed from the recorded fills via stop_policy, exactly as
            # ProtectPosition would have. A short we cannot price a stop for is a
            # mismatch: surface it (RSK-03 blocks entries) rather than emit a
            # trigger-less order the broker rejects, which would leave the short
            # naked with no signal at all.
            spec = plan.stop_specs.get((entry_id, side))
            if spec is None or spec.stop_trigger is None:
                self._events.append(ReconciliationMismatch(
                    detail=f"cannot re-place stop for {entry_id}/{side}: no stop trigger"))
                continue
            await self._broker.submit(protective_stop(
                entry_id=entry_id, right=right_of(side), contracts=spec.contracts,
                trigger=spec.stop_trigger, symbol=spec.symbol, idempotency_key=key))
            self._events.append(StopReplaced(entry_id=entry_id, side=side))

        for entry_id, side in plan.run_lex:
            self._events.append(LongSaleStarted(entry_id=entry_id, side=side))
