"""Canonical order intent — the ONE schema every broker consumes.

Before this existed, each service invented its own dict dialect (`type` vs
`order_type`, `trigger` vs `stop_trigger`, `leg: "short_put"` vs `legs: 4`) and
each broker read a different one. Paper worked, live crashed, and no test could
see it because the fakes spoke the same dialect as the emitters.

So: ONE frozen, validated type. Adapters translate it outward (doc 05 §121 —
payload translation is the ACL's job). A service cannot emit a variant dialect
because there is no other shape to emit.

Two invariants are enforced in the constructor, not by convention:

  * **qty == contracts on EVERY leg**, stops included. A 2-contract condor
    protected by a 1-contract stop leaves half the short position naked.
  * A leg is identified by exactly one of `strike` (+ intent `expiration`,
    resolved to an OCC symbol by the ACL) or an already-resolved `symbol`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

ORDER_TYPES = frozenset({"limit", "marketable_limit", "stop_market", "stop_limit"})
STOP_TYPES = frozenset({"stop_market", "stop_limit"})
PRICED_TYPES = frozenset({"limit", "marketable_limit", "stop_limit"})
ACTIONS = frozenset({"buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"})
RIGHTS = frozenset({"P", "C"})


class IntentError(ValueError):
    """A malformed order intent — refused at construction, before any broker."""


@dataclass(frozen=True)
class OrderLeg:
    right: str                      # "P" | "C"
    action: str                     # buy/sell _to_open/_to_close
    qty: int                        # == the intent's contracts (enforced below)
    strike: Decimal | None = None   # resolved to an OCC symbol by the ACL
    symbol: str | None = None       # already-resolved OCC symbol

    def __post_init__(self) -> None:
        if self.right not in RIGHTS:
            raise IntentError(f"leg right {self.right!r} not in {sorted(RIGHTS)}")
        if self.action not in ACTIONS:
            raise IntentError(f"leg action {self.action!r} not in {sorted(ACTIONS)}")
        if self.qty < 1:
            raise IntentError(f"leg qty must be >= 1, got {self.qty}")
        if (self.strike is None) == (self.symbol is None):
            raise IntentError("leg needs exactly one of `strike` or `symbol`")


@dataclass(frozen=True)
class OrderIntent:
    order_type: str
    tif: str
    legs: tuple[OrderLeg, ...]
    contracts: int
    kind: str = ""                  # iron_condor | stop | close | lex | decay | escalation
    entry_id: str = ""
    idempotency_key: str = ""       # ORD-04
    underlying: str = "SPXW"
    expiration: date | None = None
    price: Decimal | None = None        # limit / marketable_limit / stop_limit
    stop_trigger: Decimal | None = None  # stop_market / stop_limit

    def __post_init__(self) -> None:
        if self.order_type not in ORDER_TYPES:
            raise IntentError(f"order_type {self.order_type!r} not in {sorted(ORDER_TYPES)}")
        if not self.legs:
            raise IntentError("order intent needs at least one leg")
        if self.contracts < 1:
            raise IntentError(f"contracts must be >= 1, got {self.contracts}")

        # THE invariant: every leg — including a stop — carries the entry size.
        for i, leg in enumerate(self.legs):
            if leg.qty != self.contracts:
                raise IntentError(
                    f"leg {i} qty {leg.qty} != contracts {self.contracts}: a stop or leg "
                    "sized below the position would leave it partially naked")

        if self.order_type in STOP_TYPES:
            if self.stop_trigger is None:
                raise IntentError(f"{self.order_type} requires stop_trigger")
            if self.tif != "Day":  # assumption 2: option stops are Day-TIF only
                raise IntentError(f"option stop must be Day-TIF, got {self.tif!r}")
        elif self.stop_trigger is not None:
            raise IntentError(f"{self.order_type} must not carry stop_trigger")

        if self.order_type in PRICED_TYPES and self.price is None:
            raise IntentError(f"{self.order_type} requires price")
        if self.order_type == "stop_market" and self.price is not None:
            raise IntentError("stop_market must not carry price")

        if any(leg.strike is not None for leg in self.legs) and self.expiration is None:
            raise IntentError("legs identified by strike require an expiration")

    # --- convenience -----------------------------------------------------------
    @property
    def is_stop(self) -> bool:
        return self.order_type in STOP_TYPES

    def with_legs(self, legs: tuple[OrderLeg, ...]) -> "OrderIntent":
        from dataclasses import replace
        return replace(self, legs=legs)


def condor_legs(*, put_short: Decimal, put_long: Decimal, call_short: Decimal,
                call_long: Decimal, contracts: int) -> tuple[OrderLeg, ...]:
    """ORD-01: the four legs of one iron condor, all at the entry size."""
    return (
        OrderLeg(right="P", action="buy_to_open", qty=contracts, strike=put_long),
        OrderLeg(right="P", action="sell_to_open", qty=contracts, strike=put_short),
        OrderLeg(right="C", action="sell_to_open", qty=contracts, strike=call_short),
        OrderLeg(right="C", action="buy_to_open", qty=contracts, strike=call_long),
    )
