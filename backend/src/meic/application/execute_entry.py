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

from meic.application.market_calendar import trading_day
from meic.config.fee_model import FeeModel
from meic.domain.events import CondorFilled, CondorProposed, EntrySkipped, EntryWindowOpened
from meic.domain.fees import fee_for_legs
from meic.domain.ladder import RepriceLadder
from meic.domain.stop_policy import StopBasis, effective_cap_check, feasible
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

from .entry_gates import (
    FilterSnapshot, GateSnapshot, RiskSnapshot, evaluate_filters, evaluate_gates, evaluate_risk,
)
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
        entry_fill_poll_seconds: float = 2.0,
        stop_basis: StopBasis = StopBasis.TOTAL_CREDIT,
        stop_loss_pct: Decimal = Decimal("95"),
        stop_rebate_markup: Decimal = Decimal("0"),
        min_stop_distance_ticks: int = 2,
        max_effective_stop_pct: Decimal = Decimal("110"),
        underlying: str = "SPXW",
        alerts=None,   # ORD-09 cross-check mismatches are alert-only
        # CLS-03 seam (2026-07-11 wiring): the shared WorkingEntryOrders
        # registry, or None (every pre-wiring caller — behaviour unchanged).
        # The ladder is the only code that knows a pre-fill entry's broker
        # order id; publishing it here is what gives the panel's Cancel-entry
        # path (ManualClose.cancel_working) something to cancel, and the
        # registry's cancel flag is what lets the ladder stand down instead
        # of repricing an order the operator just cancelled.
        working_orders=None,
        # PNL-01: the per-contract fee table in force. None (every pre-existing
        # caller) defaults to the verified tastytrade schedule (FeeModel()) --
        # the seam, not a behaviour change for callers that don't care.
        fee_model: FeeModel | None = None,
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
        self._poll_seconds = entry_fill_poll_seconds
        self._basis = stop_basis
        self._pct = stop_loss_pct
        self._markup = stop_rebate_markup
        self._min_distance = min_stop_distance_ticks
        self._cap_pct = max_effective_stop_pct  # STP-02b cage (v1.67), doc 06 §32
        self._underlying = underlying
        self._alerts = alerts
        self._working = working_orders   # CLS-03 seam; None => no-op
        self._fee_model = fee_model or FeeModel()  # PNL-01
        # The GLOBAL stop settings, used by any row that overrides none of them.
        self.default_stop = StopParams(stop_basis, stop_loss_pct, stop_rebate_markup)

    def _skip(self, day: str, n: int, reason: str) -> EntryOutcome:
        self._events.append(EntrySkipped(date=day, entry_number=n, reason=reason))
        return EntryOutcome("SKIPPED", reason)

    # --- CLS-03 seam helpers (no-ops when no registry is wired) ----------------
    def _register(self, entry_id: str, order_id) -> None:
        if self._working is not None:
            self._working.record(entry_id, order_id)

    def _cancel_requested(self, entry_id: str) -> bool:
        return self._working is not None and self._working.cancel_requested(entry_id)

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
        put_floor: Decimal | None = None,
        call_floor: Decimal | None = None,
        filters: FilterSnapshot | None = None,
        blackout_overridden: bool = False,
    ) -> EntryOutcome:
        """THE entry path. Scheduled entries and manual ENT-09 fires both come
        through here, and `bypass_window` is the ONLY thing a manual fire changes
        (ENT-09: the window guards stale scheduled intent; a manual press is fresh
        intent by definition). Every other rail below applies unreduced — which is
        why RSK-04 lives here and not in a caller.

        `put_floor`/`call_floor` (ENT-09b v1.57): audit-only — carried through
        to the eventual `CondorFilled` so a manual fire's floors are recorded
        in the entry's events (never used to re-derive the selection here;
        the selector already applied them before `condor` was built).

        `filters` (ENT-06/CAL-05, v1.71): optional — None (every pre-v1.71
        caller, and ManualEntry's own fire path) means "no filters supplied",
        never "blocked". A SCHEDULED entry's caller (RunTradingDay/LiveRuntime)
        is the only production wiring that ever supplies one; a manual fire
        handles a calendar blackout through its OWN warn-and-acknowledge path
        (CAL-06, application/manual_entry.py) instead of this hard-skip filter
        — the two are deliberately different rails for the same tag store.

        `blackout_overridden` (CAL-06, v1.57-style audit passthrough): True
        only when ManualEntry's own CAL-06 check already ran and the operator
        acknowledged a blackout — carried through to `CondorFilled` so the
        day report/entry card can show it, mirroring `put_floor`/`call_floor`
        above. Always False for a scheduled entry.
        """
        n = condor.entry_number

        # 1. ENT-02 window (the one rule ENT-09 may bypass)
        if not bypass_window and not within_window(self._clock.now(), scheduled, self._window):
            return self._skip(day, n, "missed_window")

        # 2. ENT-03 gate chain
        reason = evaluate_gates(gates)
        if reason is not None:
            return self._skip(day, n, reason)

        # 2b. ENT-06 filters (vix_max, the static skip_dates list, CAL-05's
        # calendar blackout, min_total_credit) -- "checked at ENT-03 time,
        # each filter independently toggleable" (entry_gates.py). Entries
        # ONLY: nothing else in this codebase calls `evaluate_filters` or
        # consults the calendar tag store (CAL-05's "everything else runs
        # untouched" -- stops, LEX, TPF/TPT, decay, EOD, reconcile never
        # reach this method at all).
        if filters is not None:
            reason = evaluate_filters(filters)
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

        # 4b. STP-02b effective-percentage cage (v1.67): "SKIPS any entry whose
        # MARKUP pushes past it" -- this guards the markup's inverse-scaling
        # bite specifically, not `stop_loss_pct` itself. `stop_loss_pct` is its
        # OWN long-ratified, separately-selectable dial (95-300%, doc 06 STP-02)
        # with no cap of its own; a high pct chosen with ZERO markup is not
        # "markup pushing past" anything, so the cage only evaluates when a
        # markup is actually in force. `max_effective_stop_pct` is global-only,
        # not a per-row override (doc 06 §37's override list omits it).
        if s.markup > 0:
            cap_ok, _worst_effective_pct = effective_cap_check(
                s.basis, ticks=self._ticks,
                short_prices={"PUT": condor.put_short_mid, "CALL": condor.call_short_mid},
                net_credit=condor.mid_credit, pct=s.pct, markup=s.markup,
                cap_pct=self._cap_pct,
            )
            if not cap_ok:
                return self._skip(day, n, "markup_exceeds_cap")

        self._events.append(EntryWindowOpened(date=day, entry_number=n))
        entry_id = f"{day}#{n}"
        self._events.append(CondorProposed(
            entry_id=entry_id, put_short=condor.put_short, call_short=condor.call_short))

        # 4. ORD-01/02/03 — one 4-leg limit, repriced down to the floor
        return await self._work_order(day, n, entry_id, condor, initiator,
                                      put_floor=put_floor, call_floor=call_floor,
                                      blackout_overridden=blackout_overridden)

    async def _work_order(self, day, n, entry_id, condor: Condor,
                          initiator: str = "schedule",
                          put_floor: Decimal | None = None,
                          call_floor: Decimal | None = None,
                          blackout_overridden: bool = False) -> EntryOutcome:
        try:
            return await self._run_ladder(day, n, entry_id, condor, initiator,
                                          put_floor=put_floor, call_floor=call_floor,
                                          blackout_overridden=blackout_overridden)
        finally:
            # CLS-03 seam: however the attempt ended (fill, skip, operator
            # cancel or error), this entry no longer has a working order to
            # cancel — clear the registry entry and any spent stand-down flag.
            if self._working is not None:
                self._working.clear(entry_id)

    async def _run_ladder(self, day, n, entry_id, condor: Condor,
                          initiator: str = "schedule",
                          put_floor: Decimal | None = None,
                          call_floor: Decimal | None = None,
                          blackout_overridden: bool = False) -> EntryOutcome:
        ladder = RepriceLadder(
            start=condor.mid_credit, ticks=self._ticks,
            attempts=self._reprice_attempts, floor=condor.min_total_credit)
        rungs = ladder.prices()
        if not rungs:  # mid already below the floor
            return self._skip(day, n, "insufficient_credit")

        # 0DTE: the expiration IS today unless selection named one explicitly.
        # DAY-03: "today" is the ET trading day, not `self._clock.now()`'s own
        # (UTC) `.date()` -- see `trading_day`'s docstring.
        expiration = condor.expiration or trading_day(self._clock.now())

        working_id = None
        working_price = None
        for step in rungs:
            # CLS-03 (UC-14/TC-CLS-02, 2026-07-11 wiring): the operator
            # cancelled this WORKING entry through the panel — the broker-side
            # cancel already went out via ManualClose.cancel_working (the one
            # ratified path). Stand down instead of repricing: a replace here
            # would race that cancel, and the live adapter's cancel-then-submit
            # replace fallback could even RE-SUBMIT the very order the operator
            # just cancelled. Falls through to the shared cancel-and-confirm
            # block below, so the raced-fill guard applies here identically.
            if working_id is not None and self._cancel_requested(entry_id):
                break
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
                self._register(entry_id, working_id)   # CLS-03: publish the id
            else:
                # ORD-02 reprice — but NEVER reprice an order that has already
                # filled. A live fill registers a beat after it happens at the
                # broker; blindly repricing it cancels nothing and submits a SECOND
                # condor (2026-07-09 incident #2: margin_check_failed, and the first
                # fill went unprotected). Re-confirm not-filled immediately first.
                if await self._filled(working_id):
                    return await self._record_fill(entry_id, working_id, condor, expiration,
                                                   working_price, initiator,
                                                   blackout_overridden=blackout_overridden)
                try:
                    working_id = await self._broker.replace(working_id, intent)
                    self._register(entry_id, working_id)  # CLS-03: a replace mints a new id
                except Exception:
                    # REPRICE-RACE SWEEP (2026-07-11): the pre-check above narrows
                    # the window but does not close it — a live fill can still
                    # land in the gap between that check and this replace() call
                    # (the real broker's margin_check_failed on the duplicate, or
                    # any other replace-after-fill rejection). Re-confirm before
                    # ever propagating: if it turns out the order filled, this
                    # was in fact a fill racing the replace, not a genuine
                    # error — record it as the fill it is. A real, unrelated
                    # replace failure (not a race) still propagates unchanged.
                    if await self._filled(working_id):
                        return await self._record_fill(entry_id, working_id, condor, expiration,
                                                       working_price, initiator,
                                                       blackout_overridden=blackout_overridden)
                    if self._cancel_requested(entry_id):
                        # CLS-03: the operator's cancel landed inside this
                        # replace round trip — not a genuine error. Stand down
                        # through the shared cancel-and-confirm block below.
                        break
                    raise
            working_price = step.price

            # Paper fills are synchronous, so this returns on the FIRST poll without
            # waiting; a live fill needs a beat to register, so we POLL across the
            # reprice interval and stop the moment it fills — never waiting the whole
            # interval (that would leave the fill unprotected) and never repricing a
            # filled order.
            if await self._await_fill(working_id, self._reprice_seconds, entry_id=entry_id):
                return await self._record_fill(entry_id, working_id, condor, expiration,
                                               working_price, initiator,
                                               put_floor=put_floor, call_floor=call_floor,
                                               blackout_overridden=blackout_overridden)

        # ORD-03 / EC-ENT-05: floor reached unfilled ⇒ cancel and skip. Last guard:
        # it may have filled right at the final deadline.
        if working_id is not None:
            if await self._filled(working_id):
                return await self._record_fill(entry_id, working_id, condor, expiration,
                                               working_price, initiator,
                                               put_floor=put_floor, call_floor=call_floor,
                                               blackout_overridden=blackout_overridden)
            await self._broker.cancel(working_id)
            # REPRICE-RACE SWEEP (2026-07-11): the pre-check above narrows the
            # window but does not close it — a live fill can still land in the
            # gap between that check and this cancel() call, and neither
            # adapter's cancel() reliably reports "it was already filled"
            # (SimulatedBroker: {"result": "terminal", ...}; TastytradeAdapter:
            # {"result": "error", ...} for ANY cancel failure, fill-races
            # included). Trusting the cancel blindly here would record a
            # genuinely FILLED condor as `unfilled_at_floor` — naked, unprotected,
            # and invisible (no CondorFilled, no stop, no alert). Re-confirm
            # against the fills feed one more time, post-cancel, before giving up.
            if await self._filled(working_id):
                return await self._record_fill(entry_id, working_id, condor, expiration,
                                               working_price, initiator,
                                               put_floor=put_floor, call_floor=call_floor,
                                               blackout_overridden=blackout_overridden)
        if working_id is not None and self._cancel_requested(entry_id):
            # CLS-03: cancelled by the operator, not priced out at the floor —
            # recorded distinctly so the day report says WHY it never filled.
            return self._skip(day, n, "cancelled_by_operator")
        return self._skip(day, n, "unfilled_at_floor")

    async def _await_fill(self, working_id, seconds: float, entry_id: str | None = None) -> bool:
        """Poll for `working_id`'s fill for up to `seconds`, returning True as soon
        as it fills. The first check is immediate (synchronous paper fills return
        here with no wait); otherwise it re-checks every `entry_fill_poll_seconds`.
        Returning True is what keeps the ladder from repricing a filled order.
        An operator cancel (CLS-03) ends the wait early — False, so the rung
        loop's stand-down check runs instead of sitting out the interval."""
        deadline = self._clock.now() + timedelta(seconds=seconds)
        while True:
            if await self._filled(working_id):
                return True
            if entry_id is not None and self._cancel_requested(entry_id):
                return False
            if self._clock.now() >= deadline:
                return False
            nxt = min(deadline, self._clock.now() + timedelta(seconds=self._poll_seconds))
            await self._clock.wait_until(nxt)

    async def _record_fill(self, entry_id, working_id, condor: Condor, expiration: date,
                           fill_credit, initiator: str, *,
                           put_floor: Decimal | None = None,
                           call_floor: Decimal | None = None,
                           blackout_overridden: bool = False) -> EntryOutcome:
        legs = await self._fill_legs(working_id, condor, expiration)
        # BUG FIX (2026-07-09 incident): `fill_credit` here is only the working
        # ladder RUNG price (an estimate) — the day the rung read 3.50, the
        # broker's actual per-leg allocations netted 3.60 (sold 1.80+1.95, bought
        # 0.08+0.07), and the bot recorded the rung's 3.50. ORD-09/STP-02d: the
        # broker-ALLOCATED per-leg prices are the source of truth for what was
        # actually paid/received, so use them whenever every leg carries one.
        # Falls back to the rung estimate for the honest cases where it can't:
        # paper/simulated fills (no allocation exists) or a real fill the broker
        # reported with a leg missing its allocation.
        actual = fill_credit
        short_premium = Decimal("0")
        if legs and all(leg.price is not None for leg in legs):
            actual = (sum(leg.price for leg in legs if leg.role == "short")
                     - sum(leg.price for leg in legs if leg.role == "long"))
            # UI-14: gross premium on the shorts alone, from the SAME
            # broker-allocated per-leg prices `actual` uses above -- no new
            # I/O, no inference. Paper/simulated fills carry no allocation
            # (leg.price is None), so this stays the honest 0.00 default,
            # same guard as `actual`'s fallback above.
            short_premium = sum(leg.price for leg in legs if leg.role == "short")
        # PNL-01: fee at fill time, from the SAME already-fetched leg data
        # (role + qty per leg) -- no new broker I/O. Paper/simulated fills
        # still carry role/qty (only `price` is None there, see
        # `adapters/occ.py::simulated_fill_legs`), so paper entries get a
        # real, non-zero fee too (SIM-05: paper runs the identical pipeline).
        fee = fee_for_legs(self._fee_model, legs, opening=True) if legs else Decimal("0")
        self._events.append(CondorFilled(
            entry_id=entry_id, net_credit=actual, fee=fee, short_premium=short_premium,
            legs=legs,
            initiator=initiator,             # ENT-09 / UC-08 tagging
            at=self._clock.now().isoformat(),  # UI card: fill time
            put_floor=put_floor, call_floor=call_floor,  # ENT-09b v1.57 audit
            broker_order_id=str(working_id),  # OWN-01/OWN-03: the entry's own order id
            blackout_overridden=blackout_overridden))  # CAL-06 (v1.71) audit
        return EntryOutcome("FILLED", fill_credit=actual)

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
