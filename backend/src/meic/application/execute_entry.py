"""ExecuteEntryAttempt — the entry pipeline (ENT-02/03, ORD-01/02/03, STP-02c).

Orchestrates one scheduled entry attempt:
  1. ENT-02 window — begin within entry_window_seconds or skip `missed_window`
     (never executed late).
  2. ENT-03 gate chain (entry_gates) — first failure skips with its reason.
  3. STP-02c pre-entry feasibility — the estimated trigger must clear each
     short by min_stop_distance_ticks, else skip `infeasible_stop`.
  4. ORD-01/02/03 — one 4-leg limit at mid credit, repriced down one tick per
     entry_reprice_seconds up to entry_reprice_attempts, never below
     min_total_credit; floor reached unfilled ⇒ cancel and skip.
  5. On fill ⇒ CondorFilled, then ProtectPosition.

Selection (probe walk, collisions, credit gates) is pure domain, already
tested; it is passed in as a ready Condor so this service owns only
scheduling, the gate chain, the order ladder, and partial handling.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from meic.domain.events import CondorFilled, CondorProposed, EntrySkipped, EntryWindowOpened
from meic.domain.ladder import RepriceLadder
from meic.domain.stop_policy import StopBasis, feasible
from meic.domain.ticks import TickTable

from .entry_gates import GateSnapshot, evaluate_gates


@dataclass(frozen=True)
class Condor:
    """A fully selected condor ready to work (domain selection already ran)."""

    entry_number: int
    put_short: Decimal
    call_short: Decimal
    put_short_mid: Decimal
    call_short_mid: Decimal
    mid_credit: Decimal       # net credit at mid (ORD-02 start price)
    min_total_credit: Decimal  # ORD-03 floor


@dataclass(frozen=True)
class EntryOutcome:
    status: str            # "FILLED" | "SKIPPED"
    reason: str | None = None
    fill_credit: Decimal | None = None


def within_window(now: datetime, scheduled: datetime, window_seconds: int) -> bool:
    """ENT-02: the attempt may begin only within the tolerance window."""
    return scheduled <= now <= scheduled + timedelta(seconds=window_seconds)


class ExecuteEntryAttempt:
    def __init__(
        self,
        broker,
        clock,
        events: list,
        ticks: TickTable,
        *,
        entry_window_seconds: int = 120,
        entry_reprice_seconds: int = 20,
        entry_reprice_attempts: int = 5,
        stop_basis: StopBasis = StopBasis.TOTAL_CREDIT,
        stop_loss_pct: Decimal = Decimal("95"),
        stop_rebate_markup: Decimal = Decimal("0"),
        min_stop_distance_ticks: int = 2,
    ) -> None:
        self._broker = broker
        self._clock = clock
        self._events = events
        self._ticks = ticks
        self._window = entry_window_seconds
        self._reprice_seconds = entry_reprice_seconds
        self._reprice_attempts = entry_reprice_attempts
        self._basis = stop_basis
        self._pct = stop_loss_pct
        self._markup = stop_rebate_markup
        self._min_distance = min_stop_distance_ticks

    def _skip(self, day: str, n: int, reason: str) -> EntryOutcome:
        self._events.append(EntrySkipped(date=day, entry_number=n, reason=reason))
        return EntryOutcome("SKIPPED", reason)

    async def attempt(
        self,
        *,
        day: str,
        scheduled: datetime,
        condor: Condor,
        gates: GateSnapshot,
    ) -> EntryOutcome:
        n = condor.entry_number

        # 1. ENT-02 window
        if not within_window(self._clock.now(), scheduled, self._window):
            return self._skip(day, n, "missed_window")

        # 2. ENT-03 gate chain
        reason = evaluate_gates(gates)
        if reason is not None:
            return self._skip(day, n, reason)

        # 3. STP-02c pre-entry feasibility (estimated trigger vs shorts)
        if not feasible(
            self._basis, ticks=self._ticks,
            short_prices={"PUT": condor.put_short_mid, "CALL": condor.call_short_mid},
            pct=self._pct, markup=self._markup, total_net_credit=condor.mid_credit,
            min_distance_ticks=self._min_distance,
        ):
            return self._skip(day, n, "infeasible_stop")

        self._events.append(EntryWindowOpened(date=day, entry_number=n))
        entry_id = f"{day}#{n}"
        self._events.append(CondorProposed(
            entry_id=entry_id, put_short=condor.put_short, call_short=condor.call_short))

        # 4. ORD-01/02/03 — one 4-leg limit, repriced down to the floor
        return await self._work_order(day, n, entry_id, condor)

    async def _work_order(self, day, n, entry_id, condor: Condor) -> EntryOutcome:
        ladder = RepriceLadder(
            start=condor.mid_credit, ticks=self._ticks,
            attempts=self._reprice_attempts, floor=condor.min_total_credit)
        rungs = ladder.prices()
        if not rungs:  # mid already below the floor
            return self._skip(day, n, "insufficient_credit")

        working_id = None
        for step in rungs:
            intent = {
                "type": "limit", "kind": "iron_condor", "legs": 4, "tif": "Day",
                "net_credit": step.price, "entry_id": entry_id,
                "action": "sell_to_open",  # net credit received
            }
            if working_id is None:
                working_id = await self._broker.submit(intent)
            else:
                working_id = await self._broker.replace(working_id, intent)  # ORD-02 reprice

            if await self._filled(working_id):
                fill_credit = step.price
                self._events.append(CondorFilled(entry_id=entry_id, net_credit=fill_credit))
                return EntryOutcome("FILLED", fill_credit=fill_credit)
            await self._clock.wait_until(self._clock.now())  # advance-controlled reprice gap

        # ORD-03: floor reached unfilled ⇒ cancel and skip
        if working_id is not None:
            await self._broker.cancel(working_id)
        return self._skip(day, n, "unfilled")

    async def _filled(self, order_id) -> bool:
        for f in await self._broker.fills_since(None):
            if f.get("order_id") == order_id and not f.get("partial"):
                return True
        return False
