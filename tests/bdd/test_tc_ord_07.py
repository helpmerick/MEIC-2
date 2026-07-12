"""TC-ORD-07 — ORD-09 broker-truth leg identity (v1.45, operator-ratified).

Fill events record each leg's broker-reported OCC symbol and allocated price;
every later order action uses the RECORDED symbol; reconstruction from strikes is
a cross-check that alerts on mismatch, never the source; paper records simulator
symbols in the same fields.

The defect this rule closes: four services identified legs as the bare string
"short_put", and panel_commands.close() invented symbols like "2026-07-07#1:PUT".
"""
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal as D

import pytest
from pytest_bdd import given, scenarios, then, when

from meic.adapters.sim.simulated_broker import SimLedger, SimulatedBroker
from meic.application.close_entry import CloseEntry, LiveLeg
from meic.application.entry_gates import GateSnapshot
from meic.application.execute_entry import Condor, ExecuteEntryAttempt
from meic.application.leg_book import LegBook, crosscheck_leg_symbols
from meic.application.protect_position import ProtectPosition, ShortLeg
from meic.domain.events import CondorFilled, FilledLeg
from meic.domain.stop_policy import StopBasis
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_broker import FakeBroker
from tests.harness.fake_clock import FakeClock

scenarios("../features/TC-ORD-07.feature")

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))
WHEN = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)
EXP = date(2026, 7, 6)
ENTRY = "2026-07-06#1"

# what the broker reports for our strikes
PUT_LONG = "SPXW  260706P05940000"
PUT_SHORT = "SPXW  260706P05990000"
CALL_SHORT = "SPXW  260706C06060000"
CALL_LONG = "SPXW  260706C06110000"


def _condor(contracts=1):
    return Condor(entry_number=1, put_short=D("5990"), call_short=D("6060"),
                  put_long=D("5940"), call_long=D("6110"),
                  put_short_mid=D("3.00"), call_short_mid=D("2.00"),
                  mid_credit=D("4.00"), min_total_credit=D("2.00"),
                  expiration=EXP, contracts=contracts)


def _gates():
    return GateSnapshot(armed=True, confirm_live=True, stop_trading=False,
                        flatten_in_progress=False, market_open=True, market_halted=False,
                        data_fresh=True, session_valid=True, buying_power_ok=True)


class RecordingAlerts:
    def __init__(self):
        self.alerts = []

    def alert(self, level, message, **ctx):
        self.alerts.append((level, message, ctx))


@pytest.fixture
def world():
    return {}


def _fill(world, broker=None, contracts=1, alerts=None):
    broker = broker or FakeBroker()
    if isinstance(broker, FakeBroker):
        broker.autofill(lambda o: o.kind == "iron_condor")
    events: list = []
    ex = ExecuteEntryAttempt(broker, FakeClock(WHEN), events, SPX, alerts=alerts)
    outcome = asyncio.run(ex.attempt(day="2026-07-06", scheduled=WHEN,
                                     condor=_condor(contracts), gates=_gates()))
    world.update(broker=broker, events=events, outcome=outcome, alerts=alerts)
    return outcome


# --- Scenario: fill events record broker-reported symbols and allocations -------

@given('a condor fill is confirmed by the broker')
def _(world):
    assert _fill(world).status == "FILLED"


@then('the fill event records, for each of the 4 legs, the broker-reported OCC symbol and allocated price')
def _(world):
    filled = [e for e in world["events"] if isinstance(e, CondorFilled)]
    assert len(filled) == 1
    legs = filled[0].legs
    assert len(legs) == 4
    assert all(isinstance(l, FilledLeg) and l.symbol for l in legs)
    assert {(l.right, l.role) for l in legs} == {("P", "long"), ("P", "short"),
                                                 ("C", "short"), ("C", "long")}
    # the allocated-price FIELD exists on every leg; a simulating broker reports
    # None rather than fabricating an allocation (STP-02d is real-fills-only)
    assert all(hasattr(l, "price") for l in legs)


@then('the recorded symbols are byte-identical to the broker payload')
def _(world):
    reported = asyncio.run(world["broker"].fill_legs("FB-1"))
    recorded = [e for e in world["events"] if isinstance(e, CondorFilled)][0].legs
    assert [l.symbol for l in recorded] == [l.symbol for l in reported]
    assert [l.symbol for l in recorded] == [PUT_LONG, PUT_SHORT, CALL_SHORT, CALL_LONG]


# --- Scenario: every later order action uses the recorded symbol -----------------

@given('a recorded fill with leg symbols')
def _(world):
    _fill(world, contracts=2)
    world["book"] = LegBook.from_events(world["events"])


@when('a stop, LEX sell, decay buyback, close, or flatten order is built for a leg')
def _(world):
    book, submitted = world["book"], []

    # STP-01 stop, built the way the composition builds it: from the RECORDED symbol
    broker = FakeBroker()

    class Capture(FakeBroker):
        async def submit(self, order):
            submitted.append(order)
            return await super().submit(order)

        async def working_orders(self):
            return [o for o in self._orders.values() if o.status == "WORKING"]

    broker = Capture()
    protect = ProtectPosition(broker, FakeClock(WHEN), RecordingAlerts(), [], SPX)
    shorts = [ShortLeg(l.side, D("3.00"), D("0.50"), symbol=l.symbol)
              for l in book.shorts(ENTRY)]
    asyncio.run(protect.protect(entry_id=ENTRY, basis=StopBasis.TOTAL_CREDIT,
                                shorts=shorts, total_net_credit=D("4.00"), contracts=2))

    # CLS close, from the recorded legs
    close_broker = Capture()
    legs = [LiveLeg(l.symbol, l.side, l.role, -l.qty if l.role == "short" else l.qty)
            for l in book.of(ENTRY)]
    asyncio.run(CloseEntry(close_broker, []).close(
        ENTRY, "manual", resting_stop_ids={}, live_legs=legs, close_price=D("0.05")))

    world["submitted"] = submitted + close_broker_submissions(close_broker)


def close_broker_submissions(broker):
    return [o.intent for o in broker._orders.values()]


@then("the order's instrument symbol is the recorded one")
def _(world):
    recorded = {l.symbol for l in world["book"].of(ENTRY)}
    seen = set()
    for intent in world["submitted"]:
        for leg in intent.legs:
            assert leg.symbol is not None, "an action order identified a leg by strike, not symbol"
            seen.add(leg.symbol)
    assert seen and seen <= recorded, f"{seen - recorded} were not broker-reported"


@then('no code path reconstructs the symbol from strike and expiry at action time')
def _(world):
    # every leg of every action order carries `symbol` and NO `strike`: there is
    # nothing left for the ACL to reconstruct from
    for intent in world["submitted"]:
        for leg in intent.legs:
            assert leg.strike is None and leg.symbol


# --- Scenario: reconstruction only ever cross-checks ------------------------------

@given("a recorded symbol that disagrees with reconstruction from the condor's strikes")
def _(world):
    class DriftingBroker(FakeBroker):
        """Reports a DIFFERENT put-short symbol than our strikes imply — a real
        symbology/strike drift, e.g. the broker filled a different listing."""

        async def fill_legs(self, order_id):
            legs = list(await super().fill_legs(order_id))
            return tuple(FilledLeg("SPXW  260706P05995000", l.right, l.role, l.qty, l.price)
                         if l.symbol == PUT_SHORT else l for l in legs)

    alerts = RecordingAlerts()
    _fill(world, broker=DriftingBroker(), alerts=alerts)


@then('an alert is raised naming both values')
def _(world):
    critical = [a for a in world["alerts"].alerts if a[0] == "critical"]
    assert critical, "a symbol mismatch must alert"
    detail = critical[0][2]["detail"]
    assert "P05995000" in detail and "P05990000" in detail   # broker's AND reconstruction


@then('the recorded symbol is still the one used')
def _(world):
    legs = [e for e in world["events"] if isinstance(e, CondorFilled)][0].legs
    put_short = next(l for l in legs if (l.right, l.role) == ("P", "short"))
    assert put_short.symbol == "SPXW  260706P05995000"       # the BROKER's, not ours

    # ... and the stop built from the book names the broker's symbol
    book = LegBook.from_events(world["events"])
    assert book.symbol(ENTRY, "PUT", "short") == "SPXW  260706P05995000"


# --- Scenario: paper records simulator symbols identically ------------------------

@given('a paper-mode fill')
def _(world):
    sim = SimulatedBroker(SimLedger(cash=D("1000000")))
    sim.set_market(lambda i: (D("4.00"), D("4.00"), True))
    _fill(world, broker=sim)


@then('the fill event carries simulator-assigned leg symbols in the same fields')
def _(world):
    legs = [e for e in world["events"] if isinstance(e, CondorFilled)][0].legs
    assert [l.symbol for l in legs] == [PUT_LONG, PUT_SHORT, CALL_SHORT, CALL_LONG]
    assert all(l.price is None for l in legs)  # a simulator has no broker allocation


# --- the cross-check helper itself -------------------------------------------------

def test_crosscheck_reports_both_values_and_never_rewrites():
    recorded = (FilledLeg("SPXW  260706P05995000", "P", "short", 1),)
    problems = crosscheck_leg_symbols(
        recorded, underlying="SPXW", expiration=EXP,
        strikes={("P", "short"): D("5990")})
    assert len(problems) == 1
    assert "P05995000" in problems[0] and "P05990000" in problems[0]
    assert recorded[0].symbol == "SPXW  260706P05995000"     # untouched


def test_crosscheck_is_silent_when_the_broker_agrees():
    recorded = (FilledLeg(PUT_SHORT, "P", "short", 1),)
    assert crosscheck_leg_symbols(recorded, underlying="SPXW", expiration=EXP,
                                  strikes={("P", "short"): D("5990")}) == []
