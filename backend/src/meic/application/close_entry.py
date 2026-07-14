"""CloseEntry — the ONE canonical close path (CLS-01/02/04, v1.50 REPLACE-BASED).

Every close routes through here, differing only in the recorded initiator
(CLS-02). The broker-request sequence is a pure function of the position, NOT
the initiator — that is what makes a manual close and a TPF close byte-
identical (TC-CLS-01 scenario 1).

Procedure (CLS-01 v1.50 — supersedes the cancel-first predecessor, born from
the 2026-07-09 live incident debate: cancel-first accepted ~seconds of
nakedness; close-first (rejected) armed a two-orders-one-leg race that can
silently double-buy into an unintended long):

  (1) For EACH short leg with a resting stop: cancel/replace the stop with a
      marketable buy-to-close of ledger quantity via ONE port call,
      `broker.replace(stop_id, new_intent)` — the protection BECOMES the
      exit. There is never a moment with zero working buy orders on the short
      (naked) and never a moment with two (double-fill race); that guarantee
      lives in the broker implementation of `replace()` (see
      `tests/harness/fake_broker.py`, `adapters/sim/simulated_broker.py`,
      `adapters/tastytrade/adapter.py`), not here — CloseEntry is written
      purely against the `replace()` PORT semantics (ports.BrokerGateway).
  (2) `replace()` outcomes, classified per ORD-08 (see `cancel_taxonomy`):
        - FILLED (ORD-08a): the resting stop executed while the replace was
          in flight — that side is ALREADY closed. Route it exactly like a
          normal stop-out: emit the SAME `ShortStopped` event a live stop
          fill emits (see `SimulatedBroker.try_fill_stop`), so the SAME
          downstream SIDE_STOPPED -> LEX reaction picks it up. CloseEntry
          does not invoke RecoverLong itself — LEX-01's trigger already owns
          that — it only reports the race honestly and never submits a
          second buy on the same leg.
        - TERMINAL (ORD-08b): the stop is dead for any other reason (already
          cancelled, rejected, expired, never existed). Nothing is resting
          for it any more, so there is nothing left to race — submit the
          close directly.
        - anything else (ORD-08 "unclassifiable defaults to transient"):
          bounded retry, the ORIGINAL stop presumed still resting throughout
          (never cancelled by us on a failed attempt). Retries exhausted ->
          critical alert, the stop is left resting (protected, not naked)
          rather than risk a duplicate close.
  (3) Remaining LONG legs close via a plain marketable sell — no stops, no
      race. A long whose SHORT raced to FILLED (case 2/FILLED) is excluded:
      that side is now a genuine stop-out and its long is LEX's job, not
      CLS's. A SHORT with NO resting stop recorded (never confirmed,
      USER_UNPROTECTED, etc.) gets a direct marketable buy-to-close — no
      replace, no race to run.
  (4) Nothing of the entry is left resting or open when CLS completes (except
      a leg whose replace retries are still exhausted this call — see (2)).
  (5) Every order carries an ORD-04 idempotency key — no leg is ever closed
      twice.

Exit quantities are capped by the OwnershipLedger (OWN-04) so a shared-symbol
close only ever touches the bot's own lots.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from meic.config.fee_model import FeeModel
from meic.domain.events import EntryClosed, ShortStopped, SideClosed
from meic.domain.fees import fee_for_leg
from meic.domain.ownership import OwnershipLedger

from .cancel_taxonomy import ReplaceFilled, ReplaceTerminal
from .execute_entry import _fill_matches  # reused normalizer (2026-07-11 sweep), never a new one
from .order_intent import OrderIntent, OrderLeg, marketable_close, right_of

VALID_INITIATORS = frozenset({
    "manual", "manual_flatten", "take_profit", "take_profit_target", "eod", "decay",
    "infeasible_stop",
    # "unprotected" (STP-04 AUTO-FLATTEN, see protect_position.py `_go_unprotected`)
    # is NOT in CLS-02's operator-ratified initiator list (manual, manual_flatten,
    # take_profit, take_profit_target, eod, decay, infeasible_stop) — flagged for
    # operator ratification, kept because STP-04 demands the flatten and this is
    # the honest, distinct label for why it happened.
    "unprotected",
})

_SIDE_ORDER = {"PUT": 0, "CALL": 1}  # deterministic order (TC-CLS-01 scenario 1)


class _NoOpAlerts:
    def alert(self, level: str, message: str, **context) -> None:  # pragma: no cover - trivial
        pass


@dataclass(frozen=True)
class LiveLeg:
    symbol: str
    side: str          # "PUT" | "CALL"
    role: str          # "short" | "long"
    signed_qty: int    # bot's own position (OWN-04 capped at submit)


class CloseEntry:
    def __init__(
        self,
        broker,
        events: list,
        ledger: OwnershipLedger | None = None,
        *,
        alerts=None,
        replace_retry_attempts: int = 3,
        fee_model: FeeModel | None = None,  # PNL-01
    ) -> None:
        self._broker = broker
        self._events = events
        self._ledger = ledger or OwnershipLedger()
        self._alerts = alerts or _NoOpAlerts()
        self._replace_retry_attempts = max(1, replace_retry_attempts)
        self._fee_model = fee_model or FeeModel()

    async def close(
        self,
        entry_id: str,
        initiator: str,
        *,
        resting_stop_ids: dict[str, str],
        live_legs: list[LiveLeg],
        close_price: Decimal,
    ) -> None:
        if initiator not in VALID_INITIATORS:
            raise ValueError(f"unknown close initiator {initiator!r} (CLS-02)")

        # CLS-01 (1)/(2): shorts first — the replace-based protection-becomes-
        # exit step. Deterministic side order (PUT, then CALL) so a manual
        # close and a TPF close of identical positions are byte-identical
        # (TC-CLS-01 scenario 1) — only the recorded initiator differs.
        shorts = sorted(
            (leg for leg in live_legs if leg.role == "short"),
            key=lambda leg: _SIDE_ORDER.get(leg.side, 2),
        )
        stopped_sides: set[str] = set()  # sides that raced to FILLED (ORD-08a)

        for leg in shorts:
            qty = self._exit_qty(leg)
            intent = marketable_close(
                entry_id=entry_id, right=right_of(leg.side), contracts=qty,
                price=close_price, symbol=leg.symbol,
                idempotency_key=f"close:{entry_id}:{leg.symbol}",  # ORD-04
            )
            stop_id = resting_stop_ids.get(leg.side)
            if stop_id is None:
                # CLS-01 (3): no resting stop recorded (never confirmed,
                # USER_UNPROTECTED, ...) — direct marketable buy-to-close.
                await self._broker.submit(intent)
                self._events.append(SideClosed(entry_id=entry_id, side=leg.side))
                continue

            outcome, fill_price = await self._replace_stop(entry_id, leg.side, stop_id, intent)
            if outcome == "FILLED":
                stopped_sides.add(leg.side)
                # PNL-01: closing a short (buy-to-close) -- commission-free,
                # per the fee model (see domain/fees.py).
                fee = fee_for_leg(self._fee_model, role="short", opening=False)
                self._events.append(ShortStopped(
                    entry_id=entry_id, side=leg.side, fill=fill_price or close_price,
                    slippage=Decimal("0"), initiator="resting_stop", fee=fee))
            elif outcome == "STILL_RESTING":
                # ORD-08 transient, retries exhausted this call — the original
                # stop is untouched and still protecting the short. Never
                # naked, never double-ordered; CLS-01(4) is deferred, not
                # violated, for this leg.
                continue
            else:  # "REPLACED" or "TERMINAL" — the short is now closed.
                self._events.append(SideClosed(entry_id=entry_id, side=leg.side))

        # CLS-01 (3): remaining LONG legs — plain marketable sells, no stops,
        # no race. A long whose short raced to FILLED is excluded here: LEX
        # (triggered by the ShortStopped event above, exactly as any other
        # stop fill) owns that side's long sale, not CLS.
        longs = sorted(
            (leg for leg in live_legs if leg.role == "long" and leg.side not in stopped_sides),
            key=lambda leg: _SIDE_ORDER.get(leg.side, 2),
        )
        for leg in longs:
            qty = self._exit_qty(leg)
            await self._broker.submit(OrderIntent(
                order_type="marketable_limit", tif="Day", kind="close", entry_id=entry_id,
                contracts=qty, price=close_price,
                idempotency_key=f"close:{entry_id}:{leg.symbol}",  # ORD-04
                legs=(OrderLeg(right=right_of(leg.side), action="sell_to_close",
                               qty=qty, symbol=leg.symbol),)))
            self._events.append(SideClosed(entry_id=entry_id, side=leg.side))

        # CLS-04: record the close with its initiator (the ONLY per-initiator diff)
        self._events.append(EntryClosed(entry_id=entry_id, initiator=initiator))

    def _exit_qty(self, leg: LiveLeg) -> int:
        return self._ledger.cap_exit_qty(leg.symbol, abs(leg.signed_qty)) or abs(leg.signed_qty)

    async def _replace_stop(
        self, entry_id: str, side: str, stop_id: str, intent: OrderIntent,
    ) -> tuple[str, Decimal | None]:
        """CLS-01 (1)/(2): the ONE port call, `broker.replace()`, classified per
        ORD-08. Returns `(outcome, fill_price)` where outcome is one of
        "REPLACED" | "FILLED" | "TERMINAL" | "STILL_RESTING"."""
        last_exc: Exception | None = None
        for _ in range(self._replace_retry_attempts):
            try:
                await self._broker.replace(stop_id, intent)
                return "REPLACED", None
            except ReplaceFilled as e:
                return "FILLED", e.fill_price
            except ReplaceTerminal:
                # ORD-08b: nothing is resting for this leg any more — no race
                # left to run, so submit the close directly rather than retry
                # a replace against a dead order.
                await self._broker.submit(intent)
                return "TERMINAL", None
            except Exception as e:  # ORD-08(c) / unclassifiable -> transient
                last_exc = e
        # REPRICE-RACE SWEEP (2026-07-11): the LIVE TastytradeAdapter does not
        # yet raise ReplaceFilled for a genuine ORD-08a race (its own `replace()`
        # docstring flags this: cert's cancel-failure payloads are unverified,
        # so EVERY live replace failure — including the target having already
        # filled — lands here as "unclassifiable"). Before trusting "left
        # resting", re-confirm directly against the fills feed: if the stop
        # actually filled, "left resting" is not just imprecise, it is FALSE —
        # the leg is already closed and this must route like any other stop-out
        # (ShortStopped -> LEX), never sit in a retry-exhausted limbo repeating
        # a replace against a dead order.
        for f in await self._broker.fills_since(None):
            if _fill_matches(f, stop_id):
                price = None
                for leg in await self._broker.fill_legs(stop_id):
                    if leg.price is not None:
                        price = leg.price
                        break
                return "FILLED", price
        self._alerts.alert(
            "critical", "CLS-01 replace exhausted retries; original stop left resting",
            entry_id=entry_id, side=side, stop_id=stop_id, error=repr(last_exc))
        return "STILL_RESTING", None
