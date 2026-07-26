"""ORD-12 — a closing EXIT order can never OPEN a position.

Live evidence (external operator, 2026-07-20): two `close:` intents were
submitted and FILLED as **"Buy to Open"**, re-establishing the very long calls
the close was meant to remove. Our own PROD dry-run reproduced the mechanism
verbatim: a close with no position to close is silently converted, and the
broker says so in its own warning -- *"This order will be updated to a buy to
open order when routed"*.

WHY THIS IS A BROKER DECORATOR AND NOT A CHECK INSIDE THE SERVICES.
ENT-11(1) requires that EVERY path calls the resolver and NO path infers, and
explicitly forbids adding "a sixth path that remembers to check correctly".
A check placed in `CloseEntry` would be one such path -- LEX, DCY, the STP-03b
escalation and flatten each build their own exit and would each need their own
copy. Wrapping the BrokerGateway makes the gate structural: an exit order
cannot reach the wire without passing it, because there is no other wire.

WHERE IT MUST BE INSTALLED: innermost, at the composition root, IMMEDIATELY
after the broker is constructed and BEFORE any service captures the reference.
A service that captured the raw broker first would hold an ungated wire for the
process's whole life -- the guard would be present, wired, tested, and bypassed.

WHAT IS **NOT** AN EXIT (ORD-12 scope correction, v1.91 -- and this exclusion
is the most safety-critical line in the file). **Protective STOP PLACEMENT is
NOT an exit.** An implementation that classified a protective `buy_to_close`
stop as an exit let a lagging `positions()` read refuse all three stop
attempts, which produced UNPROTECTED_FLATTENED, whose auto-flatten was then
ALSO refused -- **a live unhedged condor with no stops, journaled as closed**:
the worst outcome this codebase can produce. The exclusion is therefore tested
on BOTH available signals (`kind == "stop"` and `order_type == "stop_market"`),
either of which is sufficient to exclude, because over-gating here is more
dangerous than under-gating.
"""
from __future__ import annotations

import logging

from meic.adapters.occ import leg_symbol
from meic.application.terminal_state import (
    ExitWouldOpen,
    TerminalStateResolver,
    TerminalStateUnknown,
)

_logger = logging.getLogger(__name__)

# The leg actions that REMOVE a position. An order carrying any of these could,
# if the position is not there, be converted by the broker into its opening
# twin -- which is the whole incident.
_CLOSING_ACTIONS = ("buy_to_close", "sell_to_close")

# The wrapper's OWN attributes -- everything else written through a guarded
# reference is forwarded to the wrapped broker (see `__setattr__`).
_WRAPPER_OWN_ATTRS = frozenset({"_broker", "_resolver", "_alerts"})


def is_exit_order(intent) -> bool:
    """Is this intent an EXIT, in ORD-12's sense?

    True only for an order that CLOSES and is not a protective stop. Read the
    two clauses in order -- the exclusion comes first deliberately, so the
    stop-placement path can never depend on how the closing-action test
    behaves for an unusual leg shape.
    """
    # ORD-12 v1.91: protective stop placement is EXPLICITLY EXCLUDED. Two
    # independent signals, either sufficient -- see the module docstring for
    # what a false positive here cost.
    if getattr(intent, "kind", "") == "stop" or getattr(intent, "is_stop", False):
        return False
    return any(str(getattr(leg, "action", "")) in _CLOSING_ACTIONS
               for leg in getattr(intent, "legs", ()) or ())


class ExitGuardedBroker:
    """A BrokerGateway that refuses any exit order which could open a position.

    Delegates every method to the wrapped broker; only `submit` and `replace`
    are gated, because they are the only two that can put a NEW order on the
    wire. `replace` is gated on its REPLACEMENT intent -- an exit that arrives
    as a stop-replacement (which is exactly how CLS-01 turns protection into an
    exit) reaches the broker through `replace`, not `submit`, and a guard that
    watched only `submit` would miss the single most common exit path in this
    system.
    """

    def __init__(self, broker, resolver: TerminalStateResolver | None = None, *,
                 alerts=None) -> None:
        self._broker = broker
        self._resolver = resolver or TerminalStateResolver(broker, alerts=alerts)
        self._alerts = alerts

    @property
    def inner(self):
        """The wrapped gateway. Named so `isinstance` checks have somewhere
        honest to look -- the same affordance `CountingBroker.inner` provides,
        and for the same reason: EC-RSK-04's "paper never constructs the live
        adapter" tests assert on the broker's TYPE, and a wrapper that hid the
        type would force those tests to be weakened rather than unwrapped.
        A guard that made a safety test easier to delete would be a bad
        trade."""
        return self._broker

    async def _assert_may_exit(self, intent) -> None:
        """Refuse the order, by RAISING, unless every closing leg provably
        holds a position (ENT-11(5): refusals raise, never a sentinel).

        Both refusal types propagate to the caller unchanged and DELIBERATELY
        undistinguished here: `ExitWouldOpen` (provably flat -- the close is a
        no-op) and `TerminalStateUnknown` (we do not know -- alert and
        re-resolve). ORD-12 treats them differently and collapsing them would
        destroy exactly the distinction ENT-11(2) exists to preserve.
        """
        if not is_exit_order(intent):
            return
        for leg in intent.legs:
            if str(getattr(leg, "action", "")) not in _CLOSING_ACTIONS:
                continue  # a non-closing leg of a mixed order opens nothing to guard
            try:
                symbol = leg_symbol(intent, leg)
            except Exception as exc:  # noqa: BLE001
                # We cannot name the leg, so we cannot resolve it -- and an
                # unresolvable leg must never pass the gate by default.
                raise TerminalStateUnknown(
                    str(getattr(leg, "symbol", None) or getattr(leg, "strike", "?")),
                    f"the exit leg's symbol could not be built ({type(exc).__name__}: {exc})"
                ) from exc
            resolution = await self._resolver.require_holds_position(symbol)
            _logger.debug("ORD-12: exit leg %s cleared -- %s", symbol, resolution.reason)

    async def submit(self, intent):
        await self._assert_may_exit(intent)
        return await self._broker.submit(intent)

    async def replace(self, id, new):
        await self._assert_may_exit(new)
        return await self._broker.replace(id, new)

    def __getattr__(self, name):
        """Every other BrokerGateway member passes straight through.

        Deliberately a delegating proxy rather than an explicit method list: a
        primitive added to the port later must not silently disappear behind
        this wrapper, and an explicit list is a thing someone has to remember
        to update -- the same class of "remembering path" ENT-11(1) forbids.
        """
        return getattr(self._broker, name)

    def __setattr__(self, name, value):
        """Writes pass through to the wrapped broker too.

        NOT symmetry for its own sake -- a read-only proxy is actively wrong
        here. `composition/runtime.py`'s drill reset does
        `comp.broker.ledger = SimLedger(...)`, which against a read-only proxy
        would land the fresh ledger on the WRAPPER while the simulator kept
        spending the old one: a reset that reports success and changes
        nothing. Any state written through a broker reference must reach the
        broker.

        WRITES MUST STAY SYMMETRIC WITH READS. A name this wrapper itself
        defines (`submit`, `replace`, `inner`) is read from the WRAPPER, so it
        must also be WRITTEN to the wrapper. Forwarding those writes to the
        inner broker while reads still resolved here produced an infinite
        recursion the first time a test replaced `broker.submit` with a spy:
        the read handed back the guard's own bound method, the write landed on
        the simulator, and the two called each other until the stack ran out.
        """
        if name in _WRAPPER_OWN_ATTRS or hasattr(type(self), name):
            object.__setattr__(self, name, value)
        else:
            setattr(self._broker, name, value)
