"""TC-OWN-11 — single-account operation alongside an existing book (v1.49).

The operator runs the bot on one account that already holds positions the bot did
not create. FOREIGN is an EXPLAINED state, not a mismatch: it is quarantined and
alerted but NEVER blocks arming or entries. Only a genuine reconciliation mismatch
(RSK-03) blocks. Foreign-occupied strikes are designed out of selection (STK-09/
OWN-08). max_day_risk caps the BOT's book only; the broker BP gate carries the
foreign book. And the bot never touches a foreign lot all day.
"""
import asyncio
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.application.eod_sweep import EndOfDaySweep
from meic.application.entry_gates import RiskSnapshot, evaluate_risk
from meic.application.reconcile_boot import entries_blocked_by_reconcile, reconcile_on_boot
from meic.domain.collision import Abort, Resolved, resolve_collisions
from meic.domain.events import ReconciliationMismatch
from meic.domain.ownership import OwnershipLedger

scenarios("../features/TC-OWN-11.feature")


class _Alerts:
    def __init__(self):
        self.alerts = []

    def alert(self, level, message, **ctx):
        self.alerts.append((level, message))


class _Broker:
    def __init__(self, positions=(), working=()):
        self._positions = list(positions)
        self._working = list(working)
        self.cancelled = []

    async def positions(self):
        return self._positions

    async def working_orders(self):
        return [o for o in self._working if getattr(o, "order_id", o.get("order_id") if isinstance(o, dict) else o) not in self.cancelled]

    async def cancel(self, oid):
        self.cancelled.append(oid)
        return {"result": "cancelled"}

    async def fills_since(self, cursor):
        return []  # this fixture never scripts a race-fill scenario

    async def submit(self, intent):
        raise AssertionError("reconcile must place NOTHING for a foreign book")


class _State:
    def __init__(self):
        self.own_ledger = {}


def _run_boot(broker):
    alerts, events, state = _Alerts(), [], _State()
    result = asyncio.run(reconcile_on_boot(
        broker=broker, events=events, state=state, alerts=alerts))
    return result, events, alerts


@pytest.fixture
def world():
    return {}


# --- Scenario 1: pre-existing positions do not block arming or entries ----------

@given("the broker account holds positions with no bot fills behind them")
def _(world):
    world["broker"] = _Broker(positions=[
        {"symbol": "SPXW  260709P07400000", "quantity": 1, "quantity_direction": "Short"},
        {"symbol": "AAPL", "quantity": 100, "quantity_direction": "Long"},
    ])


@when("startup reconcile runs")
def _(world):
    world["result"], world["events"], world["alerts"] = _run_boot(world["broker"])


@then("the positions are classified FOREIGN with a critical alert and persistent banner")
def _(world):
    r = world["result"]
    assert set(r.foreign) == {"SPXW  260709P07400000", "AAPL"}
    assert r.adopted == [] and r.shortfall == []
    # a critical alert per foreign position (the banner is driven off these)
    assert len([a for a in world["alerts"].alerts if a[0] == "critical"]) == 2


@then("arming succeeds and scheduled entries fire normally")
def _(world):
    # FOREIGN is not a mismatch -> nothing blocks. No ReconciliationMismatch logged,
    # and the durable entry-block derived from the log is clear.
    assert world["result"].entries_blocked is False
    assert not any(isinstance(e, ReconciliationMismatch) for e in world["events"])
    assert entries_blocked_by_reconcile(world["events"]) is False
    # the bot placed no order against the foreign book
    assert world["broker"].cancelled == []


# --- Scenario 2: a genuine shortfall still blocks -------------------------------

@given("the bot ledger records 2 contracts of a symbol and the broker reports 1")
def _(world):
    # ledger says the bot holds 2 short; broker shows only 1 -> SHORTFALL (RSK-03a)
    ledger = OwnershipLedger()
    ledger.apply_fill("SPXW  260709P07400000", -2)   # bot's own 2-lot short
    state = _State()
    state.own_ledger = ledger.snapshot()
    broker = _Broker(positions=[
        {"symbol": "SPXW  260709P07400000", "quantity": 1, "quantity_direction": "Short"}])
    alerts, events = _Alerts(), []
    world["result"] = asyncio.run(reconcile_on_boot(
        broker=broker, events=events, state=state, alerts=alerts))
    world["events"], world["alerts"] = events, alerts


@then("a ReconciliationMismatch is logged and RSK-03 blocks entries until reconciled")
def _(world):
    r = world["result"]
    assert r.shortfall == ["SPXW  260709P07400000"]
    assert r.entries_blocked is True                       # RSK-03 (genuine mismatch)
    assert any(isinstance(e, ReconciliationMismatch) for e in world["events"])
    assert entries_blocked_by_reconcile(world["events"]) is True


# --- Scenario 3: foreign-occupied strikes block BOTH types ----------------------

@given("a FOREIGN long at the put side's target strike")
def _(world):
    # put shorts sit BELOW spot; OTM direction is DOWN (-1). A foreign lot at the
    # target short strike must block it regardless of leg type (OWN-08).
    world["listed"] = tuple(D(str(k)) for k in range(7400, 7100, -5))  # toward OTM (down)
    world["occ"] = {D("7400"): frozenset({"foreign"})}                 # foreign at target


@when("strike selection runs")
def _(world):
    world["blocked_short"] = resolve_collisions(
        short_strike=D("7400"), long_strike=D("7350"), occupancy=world["occ"],
        listed_strikes_toward_otm=world["listed"], wing_width=D("50"), otm_direction=D("-1"))


@then("the strike is treated as blocked and the shift budget applies")
def _(world):
    # 7400 is foreign-blocked; the short shifts OTM off it (down to 7395), succeeds
    r = world["blocked_short"]
    assert isinstance(r, Resolved)
    assert r.short_strike != D("7400") and r.short_shifts >= 1

    # exhausting the shift budget on consecutive foreign strikes aborts strike_collision
    occ_wall = {D(str(k)): frozenset({"foreign"}) for k in (7400, 7395, 7390, 7385)}
    exhausted = resolve_collisions(
        short_strike=D("7400"), long_strike=D("7350"), occupancy=occ_wall,
        listed_strikes_toward_otm=world["listed"], wing_width=D("50"), otm_direction=D("-1"))
    assert isinstance(exhausted, Abort) and exhausted.reason == "strike_collision"


@then("a FOREIGN short at a candidate long strike also blocks (no stacking onto foreign lots)")
def _(world):
    # a foreign SHORT at the long's target must block the LONG too (both types)
    occ = {D("7350"): frozenset({"foreign"})}
    r = resolve_collisions(
        short_strike=D("7400"), long_strike=D("7350"), occupancy=occ,
        listed_strikes_toward_otm=world["listed"], wing_width=D("50"), otm_direction=D("-1"))
    # the long is foreign-blocked at 7350, so it shifts alone (widening the spread)
    assert isinstance(r, Resolved) and r.long_strike != D("7350") and r.long_shifts >= 1


# --- Scenario 4: max_day_risk counts only the bot's book ------------------------

@given("foreign positions of any size and no open bot entries")
def _(world):
    # RSK-04's open_worst_cases is the BOT's book only; a huge foreign book does
    # NOT appear there. The broker BP is where the foreign book constrains.
    world["risk"] = RiskSnapshot(
        new_worst_case=D("0"),
        open_worst_cases=(),                 # no BOT entries open, whatever foreign holds
        max_day_risk=D("10000"),
        buying_power=D("6000"))              # broker reality already nets foreign margin


@when("an entry whose worst case fits max_day_risk is attempted")
def _(world):
    # a 20-wide 1-lot condor: worst case (20-4)x100 = 1600, well under 10000
    world["reason"] = evaluate_risk(world["risk"].__class__(
        **{**world["risk"].__dict__, "new_worst_case": D("1600")}))


@then("RSK-04 passes — the foreign book does not consume the ceiling")
def _(world):
    assert world["reason"] is None            # 1600 fits under 10000; foreign ignored


@then("the buying-power gate still evaluates broker reality including the foreign book")
def _(world):
    # BP is the real broker number (which the foreign margin already reduced). An
    # entry needing more than that BP is refused by the ENT-03 BP rail.
    over_bp = world["risk"].__class__(
        **{**world["risk"].__dict__, "new_worst_case": D("6500")})   # > 6000 BP
    assert evaluate_risk(over_bp) == "insufficient_bp"


# --- Scenario 5: never touch survives the whole day ----------------------------

@given("trading proceeds alongside FOREIGN positions all day")
def _(world):
    # the book at EOD: one of the bot's own resting stops, and one FOREIGN working
    # order the operator placed. The sweep must touch ONLY the bot's.
    world["own_id"] = "BOT-STOP-1"
    world["foreign_id"] = "OPERATOR-ORDER-9"
    world["broker"] = _Broker(working=[
        {"order_id": world["own_id"]}, {"order_id": world["foreign_id"]}])


@then("no bot order ever references a foreign lot (OWN-04 caps at ledger)")
def _(world):
    # OWN-04: an exit can never exceed the bot's ledger quantity for a symbol. A
    # foreign symbol has 0 ledger, so any exit qty is capped to 0 — no order.
    ledger = OwnershipLedger()               # empty: foreign symbols are unknown
    assert ledger.cap_exit_qty("SPXW  260709P07400000", 5) == 0


@then("EOD verification ignores foreign working orders it did not place")
def _(world):
    alerts = _Alerts()
    sweep = EndOfDaySweep(world["broker"], alerts, own_order_ids={world["own_id"]})
    result = asyncio.run(sweep.sweep())

    assert result.cancelled == [world["own_id"]]           # only the bot's own
    assert world["foreign_id"] not in world["broker"].cancelled   # foreign untouched
    assert result.clean is True                            # a live foreign order is not "unclean"
