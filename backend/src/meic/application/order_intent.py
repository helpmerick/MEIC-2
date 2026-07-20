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

# STP-03 (v1.67, operator-ratified week-review ruling): stop_limit is
# TOMBSTONED -- MUST NOT BE BUILT, not merely unused. It is deliberately
# absent from every one of these sets so `OrderIntent(order_type="stop_limit",
# ...)` raises IntentError at construction (below) rather than succeeding: a
# stop-limit unfilled through a gap leaves a naked short with no guarantee the
# bot is alive, and the marketable stop_market path is the only one this
# system builds or proves. TC-NFR-07 scenario 2 pins this absence.
ORDER_TYPES = frozenset({"limit", "marketable_limit", "market", "stop_market"})
STOP_TYPES = frozenset({"stop_market"})
PRICED_TYPES = frozenset({"limit", "marketable_limit"})
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
    price: Decimal | None = None        # limit / marketable_limit
    stop_trigger: Decimal | None = None  # stop_market (STP-03: stop_limit tombstoned)
    replaced_from: str = ""         # DCY-04: the resting stop this one supersedes

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


def buy_to_close_leg(*, right: str, contracts: int, strike: Decimal | None = None,
                     symbol: str | None = None) -> OrderLeg:
    """The single leg every protective/closing order is built from."""
    return OrderLeg(right=right, action="buy_to_close", qty=contracts, strike=strike, symbol=symbol)


def protective_stop(
    *,
    entry_id: str,
    right: str,
    contracts: int,
    trigger: Decimal,
    strike: Decimal | None = None,
    symbol: str | None = None,
    underlying: str = "SPXW",
    expiration: date | None = None,
    idempotency_key: str = "",
    replaced_from: str = "",
    kind: str = "stop",
) -> OrderIntent:
    """STP-01/06: the broker-resting buy-to-close stop-market on ONE short.

    Every service that rests a stop (ProtectPosition, the STP-03b watchdog, the
    DCY re-protect, boot reconciliation) goes through here, so `qty == contracts`
    and Day-TIF hold by construction rather than by four separate conventions.
    """
    return OrderIntent(
        order_type="stop_market", tif="Day", contracts=contracts, kind=kind,
        entry_id=entry_id, stop_trigger=trigger, underlying=underlying,
        expiration=expiration, idempotency_key=idempotency_key, replaced_from=replaced_from,
        legs=(buy_to_close_leg(right=right, contracts=contracts, strike=strike, symbol=symbol),),
    )


def marketable_close(
    *,
    entry_id: str,
    right: str,
    contracts: int,
    strike: Decimal | None = None,
    symbol: str | None = None,
    underlying: str = "SPXW",
    expiration: date | None = None,
    idempotency_key: str = "",
    kind: str = "close",
) -> OrderIntent:
    """A marketable buy-to-close on ONE leg (CLS-01 legs, STP-03b escalation,
    DCY buy-back, LEX long recovery)."""
    return OrderIntent(
        order_type="market", tif="Day", contracts=contracts, kind=kind,
        entry_id=entry_id, underlying=underlying, expiration=expiration,
        idempotency_key=idempotency_key,
        legs=(buy_to_close_leg(right=right, contracts=contracts, strike=strike, symbol=symbol),),
    )


def working_order_qty(order) -> int | None:
    """The quantity a broker's WORKING order actually carries.

    STP-01 (v1.45) makes stop qty == short filled qty spec law, and the check has
    to work against whatever a BrokerGateway hands back: our SimOrder/FakeOrder
    (which carry the OrderIntent) and the SDK's PlacedOrder (which carries legs).
    Returns None when the shape is unreadable — the caller must treat "unknown"
    as "cannot confirm", never as "fine".
    """
    intent = getattr(order, "intent", None)
    if intent is not None and hasattr(intent, "contracts"):
        return int(intent.contracts)
    legs = getattr(order, "legs", None)
    if legs:
        try:
            return int(max(abs(Decimal(str(getattr(leg, "quantity", 0)))) for leg in legs))
        except Exception:  # noqa: BLE001 — an unreadable quantity is "unknown"
            return None
    return None


def right_of(side: str) -> str:
    """`"PUT"`/`"short_put"` -> `"P"`. The old string-keyed leg names, translated
    once, here — nowhere else."""
    s = side.lower()
    if "put" in s:
        return "P"
    if "call" in s:
        return "C"
    raise IntentError(f"cannot derive an option right from side {side!r}")


def side_of(right: str) -> str:
    """The inverse of `right_of`: `"P"` -> `"PUT"`, `"C"` -> `"CALL"`. Used to key
    a broker-reported stop order (which carries `leg.right`) back to the side
    key ("PUT"/"CALL") that `CloseEntry`'s `resting_stop_ids` mapping and
    `LiveLeg.side` use (CLS-01)."""
    if right not in RIGHTS:
        raise IntentError(f"right {right!r} not in {sorted(RIGHTS)}")
    return "PUT" if right == "P" else "CALL"
