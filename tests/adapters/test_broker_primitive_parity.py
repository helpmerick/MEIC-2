"""Broker-primitive parity — `working_orders()` ONLY (STP-04a v1.91 / NFR-09
v1.92, ENT-11(4)).

Scope note: `fill_legs` parity is DELIBERATELY EXCLUDED here. The `fill_legs`
fix ENT-11(4) also calls for is parked work-in-progress with a known-unfixed
defect; a parity test for it would either fail outright or drag parked code
into this ship. This file proves ONLY that the live `TastytradeAdapter`,
`SimulatedBroker`, and `FakeBroker` agree on "is this a WORKING order?" —
the exact question STP-04a's defect answered wrong.

THE DEFECT (established, not re-derived here): the live `working_orders()`
used to be an ALLOW-list (`status in ("live", "received")`). The pinned
vector, tests/contract/observations/03-resting-stop-placed.json, is a REAL
resting stop recorded at status "Routed" — excluded by the old allow-list,
reported by both fakes. `ProtectPosition._confirmed_qty` therefore read a
PROTECTED position as UNPROTECTED and STP-04 auto-flattened it.

NFR-09's fix inverts the direction to a DENY-list: WORKING unless provably
DEAD, with an explicit unrecognised-status branch that stays WORKING and
logs loudly rather than resolving "absent". These tests are
OBSERVATION-based per ENT-11(7): a real recorded broker payload drives the
live adapter, not a hand-built stub-vs-stub double.
"""
from __future__ import annotations

import base64
import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from meic.adapters.sim.simulated_broker import SimulatedBroker
from meic.adapters.tastytrade.adapter import TastytradeAdapter
from meic.application.order_intent import protective_stop
from tests.harness.fake_broker import FakeBroker

OBSERVATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests" / "contract" / "observations" / "03-resting-stop-placed.json"
)


def _jwt(iss: str) -> str:
    seg = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'EdDSA'})}.{seg({'iss': iss})}.sig"


CERT = _jwt("https://api.sandbox.tastyworks.com")


# --- ratified vocabulary (spec/01-strategy-rules.md STP-04a v1.91 / doc 05 NFR-09) --

WORKING_STATUSES = [
    "Received", "Live", "Routed", "Contingent", "In Flight",
    "Cancel Requested", "Replace Requested", "Partially Removed",
]
DEAD_STATUSES = ["Cancelled", "Rejected", "Expired", "Removed", "Filled"]

# enum-repr / spacing / case variants that must normalise to the same token
VARIANT_FORMS = [
    ("In Flight", "In Flight"),
    ("In Flight", "in_flight"),
    ("In Flight", "OrderStatus.IN_FLIGHT"),
    ("In Flight", "IN FLIGHT"),
    ("Partially Removed", "Partially Removed"),
    ("Partially Removed", "OrderStatus.PARTIALLY_REMOVED"),
    ("Partially Removed", "partially_removed"),
]


class _StubAccount:
    """A `get_live_orders`-only stand-in — the live adapter's `working_orders()`
    calls nothing else. No session, no network."""

    def __init__(self, orders):
        self._orders = list(orders)

    async def get_live_orders(self, session):
        return list(self._orders)


def _live_adapter(orders) -> TastytradeAdapter:
    a = TastytradeAdapter("secret", CERT, is_test=True)
    a._account = _StubAccount(orders)
    return a


def _stub_order(status, order_id="1", qty="1"):
    """SDK `PlacedOrder`-shaped stand-in: attribute access only, matching what
    `working_orders()`/`working_order_qty()` actually read (`.status`, `.id`,
    `.legs[].quantity`)."""
    return SimpleNamespace(status=status, id=order_id, legs=[SimpleNamespace(quantity=qty)])


def _resting_stop_intent(entry_id="parity#1", contracts=1):
    return protective_stop(
        entry_id=entry_id, right="P", contracts=contracts, trigger=D("0.05"),
        symbol="SPXW  260713P03000000", underlying="SPXW", expiration=date(2026, 7, 13),
        idempotency_key=f"stop:{entry_id}:PUT",
    )


# ---------------------------------------------------------------------------
# 1) OBSERVATION-DRIVEN: the pinned Routed vector, fed to all three brokers.
# ---------------------------------------------------------------------------

def test_pinned_routed_observation_is_working_across_all_three_brokers():
    """ENT-11(7): parity must be observation-based. Load the REAL recorded
    payload (not a hand-built stub) and feed it to the live adapter; feed an
    equivalent freshly-placed, still-resting stop to both fakes. All three
    MUST report the order as WORKING."""
    raw = json.loads(OBSERVATION_PATH.read_text())["observation"]
    assert raw["status"] == "Routed"  # the pinned vector, unchanged

    leg = raw["legs"][0]
    live_order = SimpleNamespace(
        status=raw["status"], id=raw["id"],
        legs=[SimpleNamespace(quantity=leg["quantity"])],
    )
    live_adapter = _live_adapter([live_order])
    import asyncio

    live_working = asyncio.run(live_adapter.working_orders())
    assert len(live_working) == 1
    assert str(live_working[0].id) == str(raw["id"])

    fake = FakeBroker()
    fake_id = asyncio.run(fake.submit(_resting_stop_intent()))
    fake_working = asyncio.run(fake.working_orders())
    assert any(getattr(o, "order_id", None) == fake_id for o in fake_working)

    sim = SimulatedBroker()
    sim_id = asyncio.run(sim.submit(_resting_stop_intent()))
    sim_working = asyncio.run(sim.working_orders())
    assert any(getattr(o, "order_id", None) == sim_id for o in sim_working)


def test_confirmed_qty_style_read_of_the_pinned_vector_never_reads_absent():
    """The exact harm path: ProtectPosition._confirmed_qty scans
    `working_orders()` for the order id and reads its quantity. Before the
    NFR-09 fix, the pinned Routed vector was invisible here -> None ->
    UNPROTECTED. It must resolve to the real filled quantity now."""
    import asyncio

    raw = json.loads(OBSERVATION_PATH.read_text())["observation"]
    leg = raw["legs"][0]
    live_order = SimpleNamespace(
        status=raw["status"], id=raw["id"],
        legs=[SimpleNamespace(quantity=leg["quantity"])],
    )
    live_adapter = _live_adapter([live_order])

    working = asyncio.run(live_adapter.working_orders())
    match = next((o for o in working if str(o.id) == str(raw["id"])), None)
    assert match is not None, "STP-04a regression: the Routed vector read as absent"


# ---------------------------------------------------------------------------
# 2) TABLE-DRIVEN: the full ratified vocabulary, all three brokers agree.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", WORKING_STATUSES)
def test_live_adapter_reports_every_working_status_as_working(status):
    import asyncio

    adapter = _live_adapter([_stub_order(status)])
    working = asyncio.run(adapter.working_orders())
    assert len(working) == 1, f"status {status!r} must be WORKING (NFR-09 ratified classification)"


@pytest.mark.parametrize("status", DEAD_STATUSES)
def test_live_adapter_reports_every_dead_status_as_not_working(status):
    import asyncio

    adapter = _live_adapter([_stub_order(status)])
    working = asyncio.run(adapter.working_orders())
    assert working == [], f"status {status!r} must be DEAD (NFR-09 ratified classification)"


@pytest.mark.parametrize("status", WORKING_STATUSES)
def test_all_three_brokers_agree_a_resting_stop_is_working(status):
    """For every WORKING broker status, the live adapter reports the order
    (driven by that exact wire status) and both fakes report their own
    still-resting analog (they do not model the broker's status vocabulary,
    but a resting order is a resting order) — the three implementations must
    never diverge on presence."""
    import asyncio

    live_working = asyncio.run(_live_adapter([_stub_order(status)]).working_orders())
    assert len(live_working) == 1

    fake = FakeBroker()
    fid = asyncio.run(fake.submit(_resting_stop_intent(entry_id=f"p-{status}")))
    assert any(getattr(o, "order_id", None) == fid for o in asyncio.run(fake.working_orders()))

    sim = SimulatedBroker()
    sid = asyncio.run(sim.submit(_resting_stop_intent(entry_id=f"p-{status}")))
    assert any(getattr(o, "order_id", None) == sid for o in asyncio.run(sim.working_orders()))


@pytest.mark.parametrize("status", DEAD_STATUSES)
def test_all_three_brokers_agree_a_dead_order_is_not_working(status):
    """For every DEAD broker status, the live adapter (driven by that exact
    wire status) excludes the order, and both fakes exclude their own
    terminal analog (FILLED, for `Filled`; CANCELLED/REJECTED otherwise)."""
    import asyncio

    live_working = asyncio.run(_live_adapter([_stub_order(status)]).working_orders())
    assert live_working == []

    fake = FakeBroker()
    fid = asyncio.run(_terminate_fake(fake, status))
    assert not any(getattr(o, "order_id", None) == fid for o in asyncio.run(fake.working_orders()))

    sim = SimulatedBroker()
    sid = asyncio.run(_terminate_sim(sim, status))
    assert not any(getattr(o, "order_id", None) == sid for o in asyncio.run(sim.working_orders()))


async def _terminate_fake(fake: FakeBroker, status: str) -> str:
    from tests.harness.fake_broker import Scripted

    if status == "Filled":
        fake.script_submit(Scripted("fill", payload={"price": "0.05"}))
        return await fake.submit(_resting_stop_intent(entry_id=f"f-{status}"))
    if status == "Rejected":
        fake.script_submit(Scripted("reject", payload={"reason": "x"}))
        return await fake.submit(_resting_stop_intent(entry_id=f"f-{status}"))
    # Cancelled, Expired, Removed: no first-class fake analog for expiry/removal —
    # CANCELLED is the fakes' one terminal-but-not-filled/rejected state, and it is
    # the correct proxy for "gone without a fill" (Expired/Removed's shared shape).
    oid = await fake.submit(_resting_stop_intent(entry_id=f"f-{status}"))
    await fake.cancel(oid)
    return oid


async def _terminate_sim(sim: SimulatedBroker, status: str) -> str:
    oid = await sim.submit(_resting_stop_intent(entry_id=f"s-{status}"))
    if status == "Filled":
        # a stop fills when the mark clears its trigger (SIM-03); trigger is 0.05
        # here, so any non-negative mark fills it deterministically.
        sim.try_fill_stop(oid, mark=D("0.05"))
    elif status == "Rejected":
        sim._orders[oid].status = "REJECTED"  # SimulatedBroker submit() never rejects on its own
    else:
        await sim.cancel(oid)  # Cancelled/Expired/Removed proxy, same rationale as the fake
    return oid


# ---------------------------------------------------------------------------
# 3) Unrecognised status: WORKING, never absent, and logged loudly.
# ---------------------------------------------------------------------------

def test_unrecognised_status_is_reported_working_never_absent(caplog):
    import asyncio
    import logging

    adapter = _live_adapter([_stub_order("SomeBrandNewStatus", order_id="999")])
    with caplog.at_level(logging.ERROR, logger="meic.adapters.tastytrade.adapter"):
        working = asyncio.run(adapter.working_orders())
    assert len(working) == 1
    assert working[0].id == "999"


def test_unrecognised_status_logs_at_error_naming_status_and_order_id(caplog):
    import asyncio
    import logging

    adapter = _live_adapter([_stub_order("SomeBrandNewStatus", order_id="999")])
    with caplog.at_level(logging.ERROR, logger="meic.adapters.tastytrade.adapter"):
        asyncio.run(adapter.working_orders())

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(error_records) == 1
    msg = error_records[0].getMessage()
    assert "SomeBrandNewStatus" in msg
    assert "999" in msg


def test_recognised_statuses_never_log_at_error(caplog):
    import asyncio
    import logging

    for status in WORKING_STATUSES + DEAD_STATUSES:
        caplog.clear()
        adapter = _live_adapter([_stub_order(status)])
        with caplog.at_level(logging.ERROR, logger="meic.adapters.tastytrade.adapter"):
            asyncio.run(adapter.working_orders())
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
            f"known status {status!r} must never log an unrecognised-status error"


# ---------------------------------------------------------------------------
# 4) Enum-form / spacing / case variants normalise identically.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("canonical, variant", VARIANT_FORMS)
def test_status_variants_normalise_to_the_same_classification(canonical, variant):
    import asyncio

    canonical_working = asyncio.run(_live_adapter([_stub_order(canonical)]).working_orders())
    variant_working = asyncio.run(_live_adapter([_stub_order(variant)]).working_orders())
    assert len(canonical_working) == len(variant_working) == 1
