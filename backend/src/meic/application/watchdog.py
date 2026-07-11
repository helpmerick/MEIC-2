"""Stop watchdog — STP-03b secondary trigger layer (v1.41).

The resting broker stop stays PRIMARY and bot-independent. The watchdog only
covers the case where the broker's trigger source (unconfirmed in cert — the
STP-05a item-2 indeterminate verdict) proves slower than the mark: if a
short's mark sits at/above its trigger with the resting stop unfilled for
grace seconds ⇒ critical alert; still unfilled at escalate seconds ⇒ the bot
fires its own marketable buy-to-close and cancels the sleeping stop.

Determinism/testability: the breach clock accumulates only across FRESH
observations (stale marks pause it, DAT-02). ORD-08 governs the race — the
escalation re-checks the resting stop and aborts if it already filled, so no
leg is ever bought twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from meic.domain.events import ShortStopped, WatchdogEscalated

from .execute_entry import _fill_matches  # reused normalizer (2026-07-11 sweep), never a new one
from .order_intent import marketable_close, right_of


@dataclass
class _Breach:
    elapsed: Decimal = Decimal("0")
    alerted: bool = False


@dataclass
class StopWatchdog:
    broker: object
    alerts: object
    events: list
    grace_seconds: Decimal = Decimal("10")
    escalate_seconds: Decimal = Decimal("20")
    _breaches: dict[tuple[str, str], _Breach] = field(default_factory=dict)
    resting_stop_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    _escalated: set = field(default_factory=set)

    def _reset(self, key) -> None:
        self._breaches.pop(key, None)

    def observe(
        self,
        *,
        entry_id: str,
        side: str,
        mark: Decimal,
        trigger: Decimal,
        seconds_since_last: Decimal,
        stop_filled: bool,
        stale: bool,
    ) -> str | None:
        """Feed one observation; returns None | 'alert' | 'escalate'.

        The caller (a QuoteHub subscriber) invokes this per mark tick with the
        wall time since the last tick. 'escalate' means: call escalate()."""
        key = (entry_id, side)
        if stop_filled:  # the resting stop did its job — silent, forever
            self._reset(key)
            return None
        if stale:  # DAT-02: pause the clock, take no action on stale marks
            return None
        if mark < trigger:  # not breaching (any more)
            self._reset(key)
            return None

        b = self._breaches.setdefault(key, _Breach())
        b.elapsed += seconds_since_last
        if b.elapsed >= self.escalate_seconds and key not in self._escalated:
            return "escalate"
        if b.elapsed >= self.grace_seconds and not b.alerted:
            b.alerted = True
            self.alerts.alert("critical", "stop watchdog: mark at/above trigger, resting stop unfilled",
                              entry_id=entry_id, side=side, mark=str(mark), trigger=str(trigger))
            return "alert"
        return None

    async def escalate(self, *, entry_id: str, side: str, mark_at_breach: Decimal, ask: Decimal,
                       symbol: str, contracts: int = 1) -> None:
        """STP-03b escalation: marketable buy-to-close + cancel the sleeping
        stop, with the ORD-08 race guard. Records calibration evidence.

        `symbol`/`contracts` identify and size the buy-back: it must close the
        WHOLE short it replaces, or the escalation leaves a naked remainder."""
        key = (entry_id, side)
        self._escalated.add(key)
        b = self._breaches.get(key, _Breach())

        # ORD-08 race: if the resting stop already filled, the stop won — abort
        # the escalation so exactly one buy-back exists.
        resting_id = self.resting_stop_ids.get(key)
        if resting_id is not None and await self._resting_stop_filled(resting_id):
            self._reset(key)
            return

        order_id = await self.broker.submit(marketable_close(
            entry_id=entry_id, right=right_of(side), contracts=contracts,
            price=ask, symbol=symbol, kind="escalation",
            idempotency_key=f"escalate:{entry_id}:{side}"))
        if resting_id is not None:
            # REPRICE-RACE SWEEP (2026-07-11): NOT WIRED LIVE today, guarded
            # preventatively. The pre-submit check above narrows the window but
            # cannot close it: the resting stop can still fill in the gap
            # between that check and the submit() just above — the broker
            # cannot undo an already-submitted marketable buy, so the best this
            # can do is recognize a genuine double-fill and say so loudly,
            # rather than silently cancelling (a no-op, since the stop is
            # already gone) and journaling a clean single escalation.
            if any(_fill_matches(f, resting_id) for f in await self.broker.fills_since(None)):
                self.alerts.alert(
                    "critical",
                    "watchdog escalation raced the resting stop to a fill — both may "
                    "have executed; verify this leg's position by hand",
                    entry_id=entry_id, side=side, resting_stop_id=resting_id,
                    escalation_order_id=order_id)
            else:
                await self.broker.cancel(resting_id)  # cancel the sleeping stop

        fill_price = ask  # marketable-limit at the ask
        self.events.append(ShortStopped(
            entry_id=entry_id, side=side, fill=fill_price, slippage=Decimal("0"),
            initiator="watchdog_escalation"))  # -> SIDE_STOPPED -> LEX
        self.events.append(WatchdogEscalated(
            entry_id=entry_id, side=side, mark_at_breach=mark_at_breach,
            elapsed_seconds=b.elapsed, fill_price=fill_price))  # calibration
        self._reset(key)

    async def _resting_stop_filled(self, resting_id: str) -> bool:
        """ORD-08 race pre-check. Matches BOTH shapes `working_orders()` can
        return — our own SimOrder/FakeOrder (`.order_id`) and the live SDK's
        `PlacedOrder` (`.id` only) — the same `.order_id`-vs-`.id` mismatch
        `protect_position._confirmed_qty` was fixed for on 2026-07-09. Matching
        only `.order_id` here made this ALWAYS report "filled" against a live
        shape (found in the 2026-07-11 sweep), which would abort every live
        escalation before it ever submitted."""
        working = await self.broker.working_orders()
        ids = {str(getattr(o, "order_id", None) or getattr(o, "id", None)) for o in working}
        return str(resting_id) not in ids
