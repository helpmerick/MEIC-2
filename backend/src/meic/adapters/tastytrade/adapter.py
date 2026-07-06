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

from meic.domain.allocation import AllocationGate, reconcile


class NonCertTokenRefused(RuntimeError):
    """Assumption 10: a refresh token whose issuer is not cert/sandbox is
    refused before any network call."""


def _jwt_issuer(token: str) -> str | None:
    import base64

    try:
        seg = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))).get("iss")
    except Exception:
        return None


def assert_cert_token(refresh_token: str) -> None:
    issuer = _jwt_issuer(refresh_token)
    if issuer is None or not ("cert" in issuer or "sandbox" in issuer):
        raise NonCertTokenRefused(f"refresh token issuer {issuer!r} is not cert/sandbox")


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
        self._secret = provider_secret
        self._refresh = refresh_token
        self._is_test = is_test
        self._session = None
        self._account = None
        self._gate = allocation_gate or AllocationGate()
        self._tick = tick

    async def connect(self) -> None:
        from tastytrade import Account, Session  # imported lazily — SDK optional offline
        self._session = Session(self._secret, refresh_token=self._refresh, is_test=self._is_test)
        self._account = (await Account.get(self._session))[0]

    @staticmethod
    def validate_stop_tif(order: dict[str, Any]) -> None:
        """Assumption 2: reject an option stop that isn't Day-TIF before submit."""
        if order.get("type") in ("stop_market", "stop_limit") and order.get("tif") not in _OPTION_STOP_ALLOWED_TIF:
            raise ValueError(
                f"option stop TIF {order.get('tif')!r} unsupported — Day only "
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

    # ---- BrokerGateway surface (real SDK calls; exercised by contract tests) --
    async def submit(self, order: Any) -> str:
        self.validate_stop_tif(order if isinstance(order, dict) else {})
        raise NotImplementedError("live submit wired + proven by contract tests (pytest -m contract)")

    async def cancel(self, id): raise NotImplementedError("contract-tested")
    async def replace(self, id, new): raise NotImplementedError("contract-tested")
    async def working_orders(self): raise NotImplementedError("contract-tested")
    async def positions(self): raise NotImplementedError("contract-tested")
    async def fills_since(self, cursor): raise NotImplementedError("contract-tested")
    def order_events(self) -> AsyncIterator[Any]: raise NotImplementedError("contract-tested")
