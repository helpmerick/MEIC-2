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
from datetime import datetime
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
        """CLS-01 replace: same-leg stop -> marketable buy-to-close in as close
        to ONE broker operation as the SDK allows.

        *** ATOMIC-REPLACE FINDING (2026-07-10, CLS-01 v1.50 implementation) ***
        This module's original comment ("cert has no atomic replace for
        these") was WRONG for this shape. The installed SDK (tastytrade
        v13, `Account.replace_order`) issues a single `PUT
        /accounts/{acct}/orders/{id}` that mutates order_type/price/
        stop_trigger while explicitly EXCLUDING `legs` from the payload
        (`model_dump_json(exclude={"legs"}, ...)`) — i.e. it keeps the SAME
        leg (same instrument, same buy_to_close action) and only swaps the
        order's shape. That is EXACTLY the CLS-01 replace (stop_market ->
        marketable_limit, same short leg): a native single-call replace
        exists and is used as the PRIMARY path below.

        UNVERIFIED against cert (flagged for the operator / contract suite,
        `pytest -m contract` — NOT run as part of this change): whether
        `replace_order` (a) 400s outright on an order-TYPE change (only
        same-type reprices — LEX/entry-ladder limit->limit — are proven live
        today), (b) errors distinctly when the target already filled
        (ORD-08a), or (c) silently no-ops. Until the contract suite
        characterizes this, the fallback below is deliberately conservative.

        RESIDUAL GAP (report prominently — this is the operator-escalation
        item): the FALLBACK path (native call raised) is cancel-then-submit,
        which is NOT atomic — cert genuinely has no atomic replace for a
        stop-market -> marketable-limit TYPE change if the native call turns
        out not to support it. Between the fallback's confirmed cancel and its
        submit there is a real, if brief, naked window. This gap is why the
        primary path exists (to avoid the fallback whenever the SDK's native
        replace actually works) and why it must be exercised in cert before
        being trusted live.
        """
        try:
            built = await self._build_order(new)
            resp = await self._account.replace_order(self._session, int(id), built)
            return str(getattr(resp, "id", "")) or ""
        except Exception:
            return await self._replace_fallback(id, new)

    async def _replace_fallback(self, id, new):
        """NOT atomic (see `replace` docstring) — the residual gap this change
        must report. Probes via `cancel()` BEFORE ever submitting a second
        order, so a stop that beat the replace to a fill is never double-
        bought; anything else that isn't a clean confirmed cancel is treated
        as ORD-08 "unclassifiable" (transient) and re-raised so the caller
        retries with the ORIGINAL stop presumed still resting, per
        ORD-08/CLS-01(2).

        KNOWN LIMITATION (part of the same residual-gap report): `cancel()`
        today returns only `{"result": "cancelled"}` or `{"result": "error",
        "error": repr(exc)}` — it does not itself classify WHY a cancel
        failed (cert's exact error shape for "already filled" vs "already
        gone" vs "rate limited" is unverified, assumption 5/ORD-08). So this
        fallback cannot yet distinguish a genuine ORD-08a fill-race from any
        other cancel failure here; it takes the SAFE default for both
        (transient — never submit a second order) rather than guess at an
        unverified error string. That means a live fill-race during THIS
        fallback path is currently handled as "retry the close later", not
        "route to LEX immediately" — correct-but-slow rather than wrong. Once
        the contract suite characterizes cert's cancel-failure payloads, this
        should classify `error` via `cancel_taxonomy.classify_cancel_failure`
        and raise `ReplaceFilled` when it resolves to "filled"."""
        cancel_result = await self.cancel(id)
        if cancel_result.get("result") == "cancelled":
            return await self.submit(new)  # confirmed dead just now — safe to submit
        # "error" (or any other shape): ambiguous -- ORD-08's unclassifiable-
        # defaults-to-transient rule. Do NOT submit a second order.
        raise RuntimeError(
            f"replace fallback: cancel of {id!r} was inconclusive "
            f"({cancel_result!r}) — ORD-08 transient, original stop presumed resting")

    async def working_orders(self) -> list[Any]:
        live = await self._account.get_live_orders(self._session)
        return [o for o in live if str(o.status).lower().split(".")[-1] in ("live", "received")]

    async def server_time(self) -> "datetime | None":
        """The broker's clock, from the `Date` header of a light authenticated call
        (DAY-03 v1.48 — measure drift against the counterparty whose clock governs
        windows). Best-effort: None means 'no reading', which the clock probe treats
        as unverified and therefore blocking. Never raises — a probe that cannot
        read the header must degrade to blocked, not crash the health loop.

        `_probe_response` is the injectable seam; the SDK's exact client is not
        pinned here, so it is contract-tested offline rather than assumed."""
        from email.utils import parsedate_to_datetime

        try:
            resp = await self._probe_response()
            date = resp.headers.get("date") if resp is not None else None
            return parsedate_to_datetime(date) if date else None
        except Exception:  # noqa: BLE001 — any failure is "no reading" (blocked), not a crash
            return None

    async def _probe_response(self):
        """A light authenticated call whose response carries the broker `Date`
        header. Overridable so `server_time` is testable without a session; in
        production it reuses the SDK's authenticated async client (the same session
        the ~60 s probe already uses — no new network path).

        SDK v13 exposes its httpx client as `Session._client` and validates the
        session with a POST to `/sessions/validate` (see `Session.validate`). Both
        matter: the previous `async_client`/GET pair silently returned None, so the
        DAY-03 clock never verified and arming was blocked forever."""
        client = getattr(self._session, "_client", None) or getattr(self._session, "async_client", None)
        if client is None:
            return None
        return await client.post("/sessions/validate")

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

    async def day_fills(self, day: str) -> list[Any]:
        """RPT-15 read-only: the broker's own Trade transactions for `day`
        (YYYY-MM-DD), straight from the SDK's transaction history --
        `Account.get_history` (a GET). No order-action capability; this is
        the ONLY method the RPT-15 `ReportReconciler` facade forwards to for
        "the day's fills" (see adapters/api/server.py's `_BrokerReadFacade`,
        which is the ONLY thing the reconciler ever sees -- never this
        adapter directly)."""
        from datetime import date as _date

        d = _date.fromisoformat(day)
        return await self._account.get_history(
            self._session, start_date=d, end_date=d, type="Trade")

    async def cash_and_fees(self, day: str) -> tuple[Decimal, Decimal]:
        """RPT-15 read-only: (cash_delta, total_fees) for `day`, from the
        broker's own transaction/fees endpoints. `cash_delta` is the sum of
        that day's Trade transactions' `net_value` (the broker's own signed
        cash effect); `total_fees` comes straight from
        `Account.get_total_fees`. Both are plain GETs -- no order-action
        capability."""
        from datetime import date as _date

        d = _date.fromisoformat(day)
        fills = await self.day_fills(day)
        cash_delta = sum((Decimal(str(t.net_value)) for t in fills), Decimal("0"))
        fees_info = await self._account.get_total_fees(self._session, day=d)
        return cash_delta, Decimal(str(fees_info.total_fees))

    async def fill_legs(self, order_id) -> tuple:
        """ORD-09: report each filled leg's BROKER-REPORTED symbol and
        BROKER-ALLOCATED fill price, byte-identical to the payload.

        Nothing here reconstructs a symbol; we copy `leg.symbol` through verbatim.
        The allocated price is the broker's own per-leg fill price — the input the
        STP-02d reconciler compares against the net fill. Where the SDK reports no
        allocation for a leg, we record None rather than guess: a fabricated
        allocation would make the reconciler agree with itself.
        """
        from meic.domain.events import FilledLeg

        live = await self._account.get_live_orders(self._session)
        order = next((o for o in live if str(getattr(o, "id", "")) == str(order_id)), None)
        if order is None:
            return ()

        legs: list[FilledLeg] = []
        for leg in getattr(order, "legs", ()) or ():
            action = str(getattr(leg, "action", "")).lower().replace(" ", "_").split(".")[-1]
            symbol = str(leg.symbol)
            legs.append(FilledLeg(
                symbol=symbol,                       # verbatim, never reconstructed
                right="P" if symbol[12:13] == "P" else "C",
                role="short" if "sell_to_open" in action else "long",
                qty=int(Decimal(str(getattr(leg, "quantity", 0)))),
                price=self._allocated_price(leg),
            ))
        return tuple(legs)

    @staticmethod
    def _allocated_price(leg) -> Decimal | None:
        """The broker's allocated fill price for one leg, or None if it reported
        none. Cert showed these do not always reconcile to the net (assumption 5),
        which is precisely why they are recorded rather than derived."""
        fills = getattr(leg, "fills", None) or ()
        prices = [Decimal(str(f.fill_price)) for f in fills if getattr(f, "fill_price", None) is not None]
        if not prices:
            return None
        quantities = [Decimal(str(getattr(f, "quantity", 1) or 1)) for f in fills]
        total_qty = sum(quantities)
        if total_qty == 0:
            return None
        return sum(p * q for p, q in zip(prices, quantities)) / total_qty  # vwap of partials

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
