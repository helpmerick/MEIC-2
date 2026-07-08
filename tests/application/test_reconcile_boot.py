"""Reconcile-on-boot (REC-02/04, EC-API-04, OWN-03/06): the bot adopts broker
truth before it may trade, and quarantines anything that isn't its own."""
import asyncio

from meic.adapters.persistence.event_store import InMemoryStateStore
from meic.application.persistent_state import PersistentState
from meic.application.reconcile import TrackedShort
from meic.application.reconcile_boot import (
    entries_blocked_by_reconcile,
    reconcile_on_boot,
)
from meic.domain.events import ReconciliationMismatch, StopReplaced


class FakeBroker:
    def __init__(self, positions=(), working=()):
        self._positions = list(positions)
        self._working = list(working)
        self.submitted = []
        self.cancelled = []

    async def positions(self):
        return self._positions

    async def working_orders(self):
        return self._working

    async def submit(self, order):
        self.submitted.append(order.get("idempotency_key"))
        return f"ord-{len(self.submitted)}"

    async def cancel(self, id):
        self.cancelled.append(id)
        return {"result": "cancelled"}


class Alerts:
    def __init__(self):
        self.alerts = []

    def alert(self, level, message, **ctx):
        self.alerts.append((level, message))


def _state(own_ledger=None):
    s = PersistentState(InMemoryStateStore())
    if own_ledger:
        s.own_ledger = own_ledger
    return s


def _run(**kw):
    return asyncio.run(reconcile_on_boot(**kw))


# --- the critical case: a FRESH bot must not touch pre-existing positions -----

def test_fresh_boot_quarantines_every_unknown_position_and_blocks_entries():
    """Empty ledger => the bot has no recorded fills, so the account's existing
    positions are FOREIGN: quarantined, alerted, and entries blocked (OWN-03)."""
    broker = FakeBroker(positions=[
        {"symbol": "SPXW_5990P", "quantity": 2, "quantity_direction": "Short"},
        {"symbol": "AAPL", "quantity": 100, "quantity_direction": "Long"},
    ])
    alerts, events, state = Alerts(), [], _state()

    r = _run(broker=broker, events=events, state=state, alerts=alerts)

    assert set(r.foreign) == {"SPXW_5990P", "AAPL"} and r.adopted == []
    assert r.entries_blocked is True                       # REC-02 -> RSK-03
    assert entries_blocked_by_reconcile(events) is True    # durable, survives restart
    assert any(isinstance(e, ReconciliationMismatch) for e in events)
    assert all(lvl == "critical" for lvl, _ in alerts.alerts) and len(alerts.alerts) == 2

    # OWN-03: quarantine means NO orders — not even for the naked short
    assert broker.submitted == [] and broker.cancelled == []


# --- crash restart: positions matching the durable ledger are adopted ---------

def test_crash_restart_adopts_own_positions_and_replaces_missing_stop():
    """The ledger (durable) accounts for the position => OWNED, adopted. Its
    short has no working stop => REC-04(3) re-places it, idempotency-keyed."""
    broker = FakeBroker(positions=[{"symbol": "SPXW_5990P", "signed_qty": -1}], working=[])
    alerts, events = Alerts(), []
    state = _state(own_ledger={"SPXW_5990P": -1})

    r = _run(broker=broker, events=events, state=state, alerts=alerts,
             tracked_shorts=[TrackedShort("e1", "PUT", "SPXW_5990P",
                                          stop_order_id=None, stop_filled=False)])

    assert r.adopted == ["SPXW_5990P"] and r.foreign == []
    assert r.entries_blocked is False and alerts.alerts == []
    assert r.stops_placed == [("e1", "PUT")]
    assert broker.submitted == ["stop:e1:PUT"]             # ORD-04 keyed, no duplicate
    assert any(isinstance(e, StopReplaced) for e in events)


def test_covered_short_is_reattached_not_replaced():
    """A short whose stop is already working at the broker is re-attached — the
    boot pass must never duplicate a live stop (REC-03/05)."""
    class Order:
        order_id = "stop-1"
    broker = FakeBroker(positions=[{"symbol": "SPXW_5990P", "signed_qty": -1}], working=[Order()])
    state = _state(own_ledger={"SPXW_5990P": -1})

    r = _run(broker=broker, events=[], state=state, alerts=Alerts(),
             tracked_shorts=[TrackedShort("e1", "PUT", "SPXW_5990P",
                                          stop_order_id="stop-1", stop_filled=False)])

    assert r.stops_placed == [] and broker.submitted == []


# --- OWN-06 ledger shortfall --------------------------------------------------

def test_ledger_shortfall_suspends_writes_down_and_never_compensates():
    """Broker shows less than the ledger (operator closed bot lots): SUSPEND,
    write the ledger down to broker truth, alert — fire no compensating order."""
    broker = FakeBroker(positions=[{"symbol": "SPXW_5990P", "signed_qty": 0}])
    alerts, events = Alerts(), []
    state = _state(own_ledger={"SPXW_5990P": -2})  # ledger says short 2, broker says flat

    r = _run(broker=broker, events=events, state=state, alerts=alerts)

    assert r.shortfall == ["SPXW_5990P"] and r.entries_blocked is True
    assert any("SUSPENDED" in m for m in r.mismatches)
    assert any(lvl == "critical" for lvl, _ in alerts.alerts)
    assert broker.submitted == []                       # never compensates automatically
    assert state.own_ledger == {}                       # written down to broker truth


def test_clean_boot_with_no_positions_blocks_nothing():
    state, events = _state(), []
    r = _run(broker=FakeBroker(), events=events, state=state, alerts=Alerts())
    assert r.entries_blocked is False and r.mismatches == []
    assert entries_blocked_by_reconcile(events) is False


# --- the parser must read a REAL tastytrade position object -------------------
# A field-name/sign bug here would silently skip real positions and fail to
# quarantine them, so this runs against the SDK's actual model class.

def test_parser_reads_real_tastytrade_position_objects():
    import pytest as _pytest
    _pytest.importorskip("tastytrade")
    from decimal import Decimal

    from tastytrade.account import CurrentPosition

    from meic.application.reconcile_boot import _symbol_and_signed_qty

    short = CurrentPosition.model_construct(
        symbol="SPXW  260707P03000000", quantity=Decimal("2"), quantity_direction="Short")
    assert _symbol_and_signed_qty(short) == ("SPXW  260707P03000000", -2)

    long_ = CurrentPosition.model_construct(
        symbol="AAPL", quantity=Decimal("100"), quantity_direction="Long")
    assert _symbol_and_signed_qty(long_) == ("AAPL", 100)

    flat = CurrentPosition.model_construct(
        symbol="X", quantity=Decimal("0"), quantity_direction="Zero")
    assert _symbol_and_signed_qty(flat) == ("X", 0)


def test_real_position_objects_are_quarantined_end_to_end():
    """Feed real CurrentPosition objects through reconcile_on_boot: a naked short
    the bot does not own must be FOREIGN, alerted, and block entries (OWN-03)."""
    import pytest as _pytest
    _pytest.importorskip("tastytrade")
    from decimal import Decimal

    from tastytrade.account import CurrentPosition

    naked_short = CurrentPosition.model_construct(
        symbol="SPXW  260707P03000000", quantity=Decimal("1"), quantity_direction="Short")
    broker = FakeBroker(positions=[naked_short])
    alerts, events, state = Alerts(), [], _state()

    r = _run(broker=broker, events=events, state=state, alerts=alerts)

    assert r.foreign == ["SPXW  260707P03000000"] and r.adopted == []
    assert r.entries_blocked is True
    assert any(lvl == "critical" for lvl, _ in alerts.alerts)
    assert broker.submitted == []  # even a foreign NAKED SHORT is alert-only
