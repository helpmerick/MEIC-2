"""DecayWatcher — decay buyback + re-inflation guard (DCY-01..04).

A tracked short whose ASK (only the ask — DCY-01) sits at/below
decay_buyback_trigger for decay_confirmation_evals consecutive valid
evaluations is bought back to kill the late re-inflation tail. Routed through
the canonical close as a SHORT-ONLY close, initiator `decay` (CLS-02 — no
second close path). Re-inflation guard (DCY-02.3): unfilled past the timeout,
or the ask rising above the trigger, cancels the buyback and RE-PLACES the
resting stop — protection restored, near-zero risk (the short was at $0.05).
The leftover long is left to expire (DCY-03, SIDE_CLOSED_DECAY).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from meic.config.fee_model import FeeModel
from meic.domain.events import DecayBuybackPlaced, EntryClosed, ShortStopped
from meic.domain.fees import fee_for_leg

from .execute_entry import _fill_matches  # reused normalizer (2026-07-11 sweep), never a new one
from .order_intent import OrderIntent, buy_to_close_leg, protective_stop, right_of


@dataclass
class DecayWatcher:
    broker: object
    events: list
    decay_buyback_trigger: Decimal = Decimal("0.05")
    decay_confirmation_evals: int = 2
    fee_model: FeeModel = field(default_factory=FeeModel)  # PNL-01
    clock: object = None  # ORD-11 (v1.67): injected clock for lifecycle `at` timestamps
    _count: int = 0

    # --- DCY-01 gates ---------------------------------------------------------
    def gate_allows(
        self,
        *,
        now_time,
        cutoff_time,
        mode: str = "AUTO",             # AUTO | MANUAL | SUSPENDED
        flatten_in_progress: bool = False,
        watcher_suspended: bool = False,  # set after a re-inflation re-placement failed under stop-trading
    ) -> bool:
        """DCY-01 gate matrix. Note: Stop Trading does NOT block (Ash's rule —
        buybacks remove risk); RTH is structural (no tracked shorts overnight)."""
        if now_time >= cutoff_time:            # not after decay_cutoff_time (15:55)
            return False
        if mode in ("MANUAL", "SUSPENDED"):    # never for MANUAL/OWN-06 SUSPENDED entries
            return False
        if flatten_in_progress:                # never while a Flatten All executes
            return False
        if watcher_suspended:                  # a failed re-placement under stop-trading suspends the watcher
            return False
        return True

    # --- DCY-01 trigger: ASK only, N consecutive valid evals ------------------
    def evaluate(self, *, ask: Decimal, stale: bool = False) -> bool:
        """True when a buyback should fire. Stale/invalid ticks reset the
        counter; only the ask can trip it."""
        if stale:
            self._count = 0
            return False
        if ask <= self.decay_buyback_trigger:
            self._count += 1
            if self._count >= self.decay_confirmation_evals:
                self._count = 0
                return True
            return False
        self._count = 0
        return False

    # --- DCY-02 procedure (short-only close, initiator decay) ------------------
    async def buyback(self, *, entry_id: str, side: str, resting_stop_id: str,
                      symbol: str, contracts: int = 1) -> str:
        """Cancel the short's resting stop (ORD-08 classified), then place a
        limit buy-to-close at the trigger. If the cancel reveals the stop
        already FILLED, abort and signal the LEX path.

        NOT WIRED LIVE today (flagged in stop_fill_watch.py's docstring). Guard
        added preventatively (2026-07-11 sweep): the `cancel.get("status")`
        check below only matches SimulatedBroker's cancel() shape
        ({"result": "terminal", "status": ...}) — the LIVE TastytradeAdapter's
        cancel() never carries a "status" key at all (assumption 5: cert's
        exact cancel-failure payload is unverified), so a live stop that raced
        this cancel to a fill would be invisible to that check and this would
        submit a SECOND buy-to-close on an already-closed leg. Re-confirm
        directly against the fills feed (reused normalizer, not a new one)
        before ever submitting.
        """
        cancel = await self.broker.cancel(resting_stop_id)
        if isinstance(cancel, dict) and cancel.get("status") == "FILLED":
            return "STOP_FILLED_RUN_LEX"  # it was a real stop-out (DCY-02.1)
        for f in await self.broker.fills_since(None):
            if _fill_matches(f, resting_stop_id):
                return "STOP_FILLED_RUN_LEX"

        order_id = await self.broker.submit(OrderIntent(
            order_type="limit", tif="Day", kind="decay", entry_id=entry_id,
            contracts=contracts, price=self.decay_buyback_trigger,
            idempotency_key=f"decay:{entry_id}:{side}",
            legs=(buy_to_close_leg(right=right_of(side), contracts=contracts, symbol=symbol),)))
        self._buyback_id = order_id
        # STP-08a (v1.61): journal the buyback's broker order id AT PLACEMENT so
        # the live detection pass (stop_fill_watch.py) can classify this order's
        # fill as SIDE_CLOSED_DECAY by id — never as a stop-out via the symbol
        # fallback (the latent hazard previously only flagged in that module's
        # docstring). Additive event; nothing else changes shape.
        self.events.append(DecayBuybackPlaced(
            entry_id=entry_id, side=side, broker_order_id=str(order_id),
            price=self.decay_buyback_trigger))
        return order_id

    async def complete(self, *, entry_id: str, side: str) -> None:
        """Buyback filled ⇒ side = SIDE_CLOSED_DECAY (long left to expire,
        DCY-03), recorded as a `decay` close (CLS-04)."""
        # PNL-01: closing a short (buy-to-close) -- commission-free. Per-share
        # (see domain/fees.py) -- never scaled by contracts here.
        fee = fee_for_leg(self.fee_model, role="short", opening=False)
        at = self.clock.now().isoformat() if self.clock is not None else None  # ORD-11 (v1.67)
        self.events.append(ShortStopped(
            entry_id=entry_id, side=side, fill=self.decay_buyback_trigger,
            slippage=Decimal("0"), initiator="decay", fee=fee, at=at))
        self.events.append(EntryClosed(entry_id=entry_id, initiator="decay", at=at))

    # --- DCY-02.3 re-inflation guard ------------------------------------------
    async def reinflation_guard(
        self, *, entry_id: str, side: str, buyback_id: str, resting_stop_id: str,
        current_ask: Decimal, unfilled: bool, symbol: str, trigger: Decimal,
        contracts: int = 1,
    ) -> str:
        """If the buyback is unfilled past the timeout OR the ask rose above
        the trigger: cancel the buyback and RE-PLACE the resting stop. Returns
        the outcome. The re-placed stop id lets ProtectPosition/STP-04 confirm.

        `trigger` is the short's ORIGINAL stop trigger — re-protecting restores
        the stop that was cancelled, it does not invent a new one. (Before v1.44
        this emitted a stop-market with no trigger at all, which no broker
        accepts: the guard would have left the short unprotected.)

        NOT WIRED LIVE today. Guard added preventatively (2026-07-11 sweep):
        the buyback can itself fill in the window between the caller's
        `unfilled`/`current_ask` read and this cancel — re-placing a stop on a
        leg that is, in fact, already closed would rest a phantom order with
        no position behind it and never record the close at all (no
        ShortStopped, no EntryClosed). Re-confirm against the fills feed
        before cancelling/re-protecting.
        """
        if not unfilled and current_ask <= self.decay_buyback_trigger:
            return "BUYBACK_STILL_LIVE"
        for f in await self.broker.fills_since(None):
            if _fill_matches(f, buyback_id):
                return "BUYBACK_ALREADY_FILLED"
        await self.broker.cancel(buyback_id)
        new_stop = await self.broker.submit(protective_stop(
            entry_id=entry_id, right=right_of(side), contracts=contracts,
            trigger=trigger, symbol=symbol, replaced_from=resting_stop_id,
            idempotency_key=f"reprotect:{entry_id}:{side}"))
        return f"REPROTECTED:{new_stop}"
