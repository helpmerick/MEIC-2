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

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from meic.domain.events import CondorFilled, CondorProposed, EntrySkipped, EntryWindowOpened
from meic.domain.ladder import RepriceLadder
from meic.domain.stop_policy import StopBasis, feasible
from meic.domain.ticks import TickTable

from meic.domain.risk import worst_case_loss


def _fill_matches(fill, order_id) -> bool:
    """A non-partial fill record for `order_id`. Broker shapes differ: the paper
    SimulatedBroker yields dicts (`{"order_id": ..., "partial": ...}`); the live
    TastytradeAdapter yields SDK order OBJECTS (`.id`, `.status`). Treating one as
    the other is what left a filled condor unprotected on 2026-07-09 — a live fill
    object has no `.get`, so fill-confirmation crashed AFTER the order filled but
    BEFORE stops were placed. This normalises both."""
    if isinstance(fill, dict):
        fid = fill.get("order_id") or fill.get("id")
        partial = bool(fill.get("partial"))
    else:
        fid = getattr(fill, "order_id", None) or getattr(fill, "id", None)
        partial = "partial" in str(getattr(fill, "status", "")).lower()
    return str(fid) == str(order_id) and not partial

from .entry_gates import GateSnapshot, RiskSnapshot, evaluate_gates, evaluate_risk
from .leg_book import crosscheck_leg_symbols
from .order_intent import OrderIntent, condor_legs


@dataclass(frozen=True)
class Condor:
    """A fully selected condor ready to work (domain selection already ran).

    Carries everything the ACL needs to build the 4-leg order: both shorts, both
    wings, the expiration, and this row's own size (ENT-04, per-entry since
    v1.44). Wings default to short ± 50 for the offline/legacy cases that only
    supply shorts; live selection always sets them explicitly.
    """

    entry_number: int
    put_short: Decimal
    call_short: Decimal
    put_short_mid: Decimal
    call_short_mid: Decimal
    mid_credit: Decimal       # net credit at mid (ORD-02 start price)
    min_total_credit: Decimal  # ORD-03 floor
    put_long: Decimal | None = None   # STK-03 wing; defaults to put_short - 50
    call_long: Decimal | None = None  # defaults to call_short + 50
    expiration: date | None = None    # 0DTE; the ACL resolves legs to OCC symbols
    contracts: int = 1                # ENT-04: this row's own size (v1.44)

    @property
    def put_wing(self) -> Decimal:
        return self.put_long if self.put_long is not None else self.put_short - Decimal("50")

    @property
    def call_wing(self) -> Decimal:
        return self.call_long if self.call_long is not None else self.call_short + Decimal("50")


@dataclass(frozen=True)
class StopParams:
    """The stop settings of ONE schedule row (doc 06 §37 per-entry overrides).

    `stop_basis`, `stop_loss_pct` and `stop_rebate_markup` are per-entry since
    v1.44, so they cannot live on the service — a row that overrides them must
    have its OWN feasibility check (STP-02c) and its OWN stop triggers, or the
    override would be silently ignored everywhere it mattered.
    """

    basis: StopBasis
    pct: Decimal
    markup: Decimal = Decimal("0")


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
        underlying: str = "SPXW",
        alerts=None,   # ORD-09 cross-check mismatches are alert-only
    ) -> None:
        # NOTE (v1.44): there is deliberately no `contracts_per_entry` here. ENT-04
        # made contracts a PER-ENTRY value — it rides on the Condor (the schedule
        # row), never on the service. `contracts_per_entry` survives in config only
        # as the UI's row pre-fill.
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
        self._underlying = underlying
        self._alerts = alerts
        # The GLOBAL stop settings, used by any row that overrides none of them.
        self.default_stop = StopParams(stop_basis, stop_loss_pct, stop_rebate_markup)

    def _skip(self, day: str, n: int, reason: str) -> EntryOutcome:
        self._events.append(EntrySkipped(date=day, entry_number=n, reason=reason))
        return EntryOutcome("SKIPPED", reason)

    @staticmethod
    def worst_case(condor: Condor) -> Decimal:
        """RSK-04: this condor's structural worst case, at its own size.

        The wider wing governs: only one side can settle in the money, but we do
        not get to choose which. Also what UI-22's confirmation dialog shows.
        """
        width = max(condor.put_short - condor.put_wing, condor.call_wing - condor.call_short)
        return worst_case_loss(width, condor.mid_credit, contracts=condor.contracts)

    async def attempt(
        self,
        *,
        day: str,
        scheduled: datetime,
        condor: Condor,
        gates: GateSnapshot,
        risk: RiskSnapshot | None = None,
        bypass_window: bool = False,
        stop: StopParams | None = None,
        initiator: str = "schedule",
    ) -> EntryOutcome:
        """THE entry path. Scheduled entries and manual ENT-09 fires both come
        through here, and `bypass_window` is the ONLY thing a manual fire changes
        (ENT-09: the window guards stale scheduled intent; a manual press is fresh
        intent by definition). Every other rail below applies unreduced — which is
        why RSK-04 lives here and not in a caller.
        """
        n = condor.entry_number

        # 1. ENT-02 window (the one rule ENT-09 may bypass)
        if not bypass_window and not within_window(self._clock.now(), scheduled, self._window):
            return self._skip(day, n, "missed_window")

        # 2. ENT-03 gate chain
        reason = evaluate_gates(gates)
        if reason is not None:
            return self._skip(day, n, reason)

        # 3. RSK-08 order cap, then RSK-04 max exposure. Priced from THIS condor's
        # own width/credit/contracts — never from a number a caller passed in.
        if risk is not None:
            reason = evaluate_risk(replace(risk, new_worst_case=self.worst_case(condor)))
            if reason is not None:
                return self._skip(day, n, reason)

        # 4. STP-02c pre-entry feasibility, against THIS ROW's stop settings
        s = stop or self.default_stop
        if not feasible(
            s.basis, ticks=self._ticks,
            short_prices={"PUT": condor.put_short_mid, "CALL": condor.call_short_mid},
            pct=s.pct, markup=s.markup, total_net_credit=condor.mid_credit,
            min_distance_ticks=self._min_distance,
        ):
            return self._skip(day, n, "infeasible_stop")

        self._events.append(EntryWindowOpened(date=day, entry_number=n))
        entry_id = f"{day}#{n}"
        self._events.append(CondorProposed(
            entry_id=entry_id, put_short=condor.put_short, call_short=condor.call_short))

        # 4. ORD-01/02/03 — one 4-leg limit, repriced down to the floor
        return await self._work_order(day, n, entry_id, condor, initiator)

    async def _work_order(self, day, n, entry_id, condor: Condor,
                          initiator: str = "schedule") -> EntryOutcome:
        ladder = RepriceLadder(
            start=condor.mid_credit, ticks=self._ticks,
            attempts=self._reprice_attempts, floor=condor.min_total_credit)
        rungs = ladder.prices()
        if not rungs:  # mid already below the floor
            return self._skip(day, n, "insufficient_credit")

        # 0DTE: the expiration IS today unless selection named one explicitly.
        expiration = condor.expiration or self._clock.now().date()

        working_id = None
        for step in rungs:
            # ENT-04 (v1.44): the size is THIS ROW's `contracts`, not a global knob.
            intent = OrderIntent(
                order_type="limit", tif="Day", kind="iron_condor", entry_id=entry_id,
                contracts=condor.contracts, price=step.price,
                underlying=self._underlying, expiration=expiration,
                idempotency_key=f"entry:{entry_id}",  # ORD-04
                legs=condor_legs(
                    put_short=condor.put_short, put_long=condor.put_wing,
                    call_short=condor.call_short, call_long=condor.call_wing,
                    contracts=condor.contracts),
            )
            if working_id is None:
                working_id = await self._broker.submit(intent)
            else:
                working_id = await self._broker.replace(working_id, intent)  # ORD-02 reprice

            if await self._filled(working_id):
                fill_credit = step.price
                legs = await self._fill_legs(working_id, condor, expiration)
                self._events.append(CondorFilled(
                    entry_id=entry_id, net_credit=fill_credit, legs=legs,
                    initiator=initiator))   # ENT-09 / UC-08 tagging
                return EntryOutcome("FILLED", fill_credit=fill_credit)
            await self._clock.wait_until(self._clock.now())  # advance-controlled reprice gap

        # ORD-03 / EC-ENT-05: floor reached unfilled ⇒ cancel and skip
        if working_id is not None:
            await self._broker.cancel(working_id)
        return self._skip(day, n, "unfilled_at_floor")

    async def _fill_legs(self, order_id, condor: Condor, expiration: date) -> tuple:
        """ORD-09: record what the BROKER said it filled.

        The cross-check reconstructs each symbol from the strikes we asked for and
        ALERTS on mismatch — it never overwrites. A disagreement means our
        symbology, or our idea of the strikes, has drifted from the broker's; the
        one thing we must not do is silently "correct" the broker and then send
        stops to an instrument it never filled.
        """
        legs = await self._broker.fill_legs(order_id)
        if not legs:
            return ()

        problems = crosscheck_leg_symbols(
            legs, underlying=self._underlying, expiration=expiration,
            strikes={("P", "short"): condor.put_short, ("P", "long"): condor.put_wing,
                     ("C", "short"): condor.call_short, ("C", "long"): condor.call_wing})
        if problems and self._alerts is not None:
            self._alerts.alert("critical", "ORD-09 leg symbol mismatch (using the broker's)",
                               entry_id=f"{order_id}", detail="; ".join(problems))
        return legs

    async def _filled(self, order_id) -> bool:
        for f in await self._broker.fills_since(None):
            if _fill_matches(f, order_id):
                return True
        return False
