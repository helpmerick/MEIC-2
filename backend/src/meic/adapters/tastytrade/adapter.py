"""TastytradeAdapter — the live BrokerGateway (doc 05 §6), built to the ten
Phase-2 cert assumptions (operator-ratified, v1.43).

Binding assumptions this adapter encodes:
  1. single-leg SPXW stop-markets ARE supported
  2. option stop-markets are Day-TIF ONLY (never GTC/GTD — hard reject)
  3. resting stops persist across session death (broker-side)
  4. trigger source indeterminate -> the STP-03b watchdog is fed live marks
  5. per-leg allocations may not reconcile -> log an allocation record on every
     REAL fill (all bases); per_side selection is gated elsewhere (STP-02d)
  6. SDK v13 = OAuth2 only, fully async
  7. cert enforces production-grade validation -> rejections are real
  10. refuse a non-cert refresh token locally BEFORE any network call

Economics stay with the fakes/SIM; contract tests (pytest -m contract) prove
this wiring against cert. The domain never imports this module — it sees only
the BrokerGateway port.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, AsyncIterator

from meic.adapters.tastytrade.occ import occ_symbol
from meic.application.order_intent import OrderIntent
from meic.domain.allocation import AllocationGate, reconcile


class NonCertTokenRefused(RuntimeError):
    """Assumption 10: a refresh token whose issuer is not cert/sandbox is
    refused before any network call."""


class NonProductionTokenRefused(RuntimeError):
    """The mirror guard: a token whose issuer is NOT production was slotted into
    the production wiring. Fail loudly rather than silently authenticate against
    the wrong environment (a cert token in TT_PROD_* is a configuration bug)."""


def _jwt_issuer(token: str) -> str | None:
    import base64

    try:
        seg = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))).get("iss")
    except Exception:
        return None


def _is_cert_issuer(issuer: str | None) -> bool:
    return bool(issuer) and ("cert" in issuer or "sandbox" in issuer)


def assert_cert_token(refresh_token: str) -> None:
    issuer = _jwt_issuer(refresh_token)
    if not _is_cert_issuer(issuer):
        raise NonCertTokenRefused(f"refresh token issuer {issuer!r} is not cert/sandbox")


def assert_production_token(refresh_token: str) -> None:
    """Symmetric to assert_cert_token. The live/production wiring must carry a
    PRODUCTION token: a missing/undecodable issuer, or a cert/sandbox one, is a
    misconfiguration and is refused before any network call. Without this, a
    cert token in the production slot would fail late and confusingly — or a
    fat-fingered env could point real-money wiring at the wrong environment."""
    issuer = _jwt_issuer(refresh_token)
    if issuer is None:
        raise NonProductionTokenRefused("refresh token has no decodable issuer")
    if _is_cert_issuer(issuer):
        raise NonProductionTokenRefused(
            f"refresh token issuer {issuer!r} is CERT/SANDBOX, not production — "
            "a cert token was slotted into the production wiring")


# Option stop-markets are Day-TIF only (assumption 2). Any other TIF on an
# option stop is a hard client-side reject — never sent to the broker.
_OPTION_STOP_ALLOWED_TIF = {"Day"}


class TastytradeAdapter:
    """Implements the BrokerGateway port. Construction is I/O-free; connect()
    establishes the SDK session (cert unless explicitly live-wired)."""

    def __init__(
        self,
        provider_secret: str,
        refresh_token: str,
        *,
        is_test: bool = True,
        allocation_gate: AllocationGate | None = None,
        tick: Decimal = Decimal("0.05"),
    ) -> None:
        if is_test:  # paper/contract wiring must never carry a production token
            assert_cert_token(refresh_token)  # assumption 10 — before any network call
        else:  # production wiring must never carry a cert token (mirror guard)
            assert_production_token(refresh_token)
        self._secret = provider_secret
        self._refresh = refresh_token
        self._is_test = is_test
        self._session = None
        self._account = None
        self._gate = allocation_gate or AllocationGate()
        self._tick = tick

    async def connect(self, account_number: str | None = None) -> None:
        from tastytrade import Account, Session  # imported lazily — SDK optional offline
        self._session = Session(self._secret, refresh_token=self._refresh, is_test=self._is_test)
        accounts = await Account.get(self._session)
        if account_number:
            self._account = next(a for a in accounts if a.account_number == account_number)
        else:
            self._account = accounts[0]

    # ---- intent translation (ACL) --------------------------------------------
    async def _option_for(self, symbol: str):
        """Resolve an OCC symbol to the SDK instrument. Overridable so the ACL
        can be contract-tested without a session (see the intent-contract suite)."""
        from tastytrade.instruments import Option
        return await Option.get(self._session, symbol)

    async def _build_order(self, intent: OrderIntent):
        """Translate the canonical OrderIntent into a tastytrade NewOrder.

        Payload translation is the ACL's job (doc 05 §121) — including resolving
        each leg's (underlying, expiration, right, strike) to an OCC symbol. The
        application layer speaks strikes; only this adapter knows symbology.

        Every leg is sized at `intent.contracts` (the OrderIntent constructor
        already refuses any other shape), so a stop can never be placed smaller
        than the position it protects.
        """
        from decimal import Decimal as D

        from tastytrade.order import NewOrder, OrderAction, OrderTimeInForce, OrderType

        self.validate_stop_tif(intent)  # assumption 2 — before building anything

        type_map = {"stop_market": OrderType.STOP, "stop_limit": OrderType.STOP_LIMIT,
                    "limit": OrderType.LIMIT, "marketable_limit": OrderType.MARKETABLE_LIMIT}
        action_map = {a.value.lower().replace(" ", "_"): a for a in OrderAction}
        tif_map = {"Day": OrderTimeInForce.DAY, "GTC": OrderTimeInForce.GTC}

        legs = []
        for leg in intent.legs:
            symbol = leg.symbol or occ_symbol(
                intent.underlying, intent.expiration, leg.right, leg.strike)
            opt = await self._option_for(symbol)
            legs.append(opt.build_leg(D(leg.qty), action_map[leg.action]))

        kwargs: dict[str, Any] = dict(
            time_in_force=tif_map.get(intent.tif, OrderTimeInForce.DAY),
            order_type=type_map[intent.order_type],
            legs=legs,
        )
        if intent.stop_trigger is not None:
            kwargs["stop_trigger"] = D(str(intent.stop_trigger))
        if intent.price is not None:
            kwargs["price"] = D(str(intent.price))
        return NewOrder(**kwargs)

    @staticmethod
    def validate_stop_tif(intent: OrderIntent) -> None:
        """Assumption 2: reject an option stop that isn't Day-TIF before submit.
        (OrderIntent already refuses this at construction; kept as the adapter's
        own last line of defence against a hand-built intent.)"""
        if intent.order_type in ("stop_market", "stop_limit") and intent.tif not in _OPTION_STOP_ALLOWED_TIF:
            raise ValueError(
                f"option stop TIF {intent.tif!r} unsupported — Day only "
                "(cert: tif_no_stop_market_gtc_options)")

    def record_fill_allocation(
        self, allocated_leg_prices: list[Decimal], net_fill: Decimal,
        *, legs_traded_at_zero: frozenset[int] = frozenset(),
    ):
        """Assumption 5 / STP-02d.2: log an allocation record on every REAL
        fill, all bases. Never called for paper fills (SimulatedBroker)."""
        rec = reconcile(allocated_leg_prices, net_fill, tick=self._tick,
                        legs_that_traded_at_zero=legs_traded_at_zero)
        self._gate.observe(rec)
        return rec

    # ---- BrokerGateway surface (real SDK calls; proven by contract tests) -----
    async def submit(self, order: OrderIntent) -> str:
        if not isinstance(order, OrderIntent):  # one schema, all brokers
            raise TypeError(f"TastytradeAdapter.submit expects an OrderIntent, got {type(order).__name__}")
        new = await self._build_order(order)
        resp = await self._account.place_order(self._session, new, dry_run=False)
        return str(resp.order.id) if resp.order else ""

    async def dry_run(self, order: OrderIntent):
        """Assumption 1/2/7: validate an order against cert without placing it."""
        new = await self._build_order(order)
        return await self._account.place_order(self._session, new, dry_run=True)

    async def cancel(self, id) -> dict[str, Any]:
        try:
            await self._account.delete_order(self._session, int(id))
            return {"result": "cancelled"}
        except Exception as e:  # ORD-08 classification is the caller's job
            return {"result": "error", "error": repr(e)}

    async def replace(self, id, new):
        await self.cancel(id)  # cert has no atomic replace for these; confirm-cancel then submit
        return await self.submit(new)

    async def working_orders(self) -> list[Any]:
        live = await self._account.get_live_orders(self._session)
        return [o for o in live if str(o.status).lower().split(".")[-1] in ("live", "received")]

    async def positions(self) -> list[Any]:
        return await self._account.get_positions(self._session)

    async def buying_power(self) -> Decimal:
        """Options buying power (ENT-03 BP gate / RSK-04). `derivative_buying_power`
        is the figure that governs an options spread, not equity BP."""
        balances = await self._account.get_balances(self._session)
        return Decimal(str(balances.derivative_buying_power))

    async def fills_since(self, cursor) -> list[Any]:
        live = await self._account.get_live_orders(self._session)
        return [o for o in live if str(o.status).lower().endswith("filled")]

    async def order_events(self) -> AsyncIterator[dict[str, Any]]:
        """Account order-status stream (STP-04/ORD-05/LEX-01). Uses the
        AlertStreamer (account WebSocket) — NOT DXLink — yielding normalized
        order-status events. Order state is driven by these events, never
        assumed (ORD-05)."""
        from tastytrade import AlertStreamer
        from tastytrade.order import PlacedOrder

        async with AlertStreamer(self._session) as streamer:
            await streamer.subscribe_accounts([self._account])
            async for order in streamer.listen(PlacedOrder):
                yield {
                    "type": "order_status",
                    "order_id": str(getattr(order, "id", "")),
                    "status": str(order.status).split(".")[-1].lower(),
                    "raw": order,
                }
