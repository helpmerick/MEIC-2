"""TC-SIM-03 (SIM-04 money): an entry fill posts credit minus per-leg fees;
open entries consume margin per the spread requirement and release on close;
insufficient simulated BP skips the entry (rejected_bp); settlement posts
against the real closing level; the ledger survives container restart (REC-07)."""
import asyncio
from decimal import Decimal as D

from meic.adapters.sim.simulated_broker import SimLedger, SimulatedBroker, spread_margin
from tests.harness.intents import condor_intent


def _fill_credit(order):
    """A real-market snapshot that trades through a credit entry limit."""
    limit = order.price
    return (limit, limit + D("0.05"), True)  # natural meets the limit -> fills


def test_tc_sim_03_entry_fill_posts_credit_minus_per_leg_fees():
    ledger = SimLedger(cash=D("100000"))
    b = SimulatedBroker(ledger, fee_per_leg=D("0.65"))
    b.set_market(_fill_credit)
    asyncio.run(b.submit(condor_intent("4.00")))
    # credit 4.00 × 100 − (4 legs × 0.65) = 400.00 − 2.60
    assert ledger.cash == D("100397.40")


def test_tc_sim_03_margin_consumed_on_open_and_released_on_close():
    ledger = SimLedger(cash=D("100000"))
    margin = spread_margin(width=D("50"), net_credit=D("4.00"))  # (50−4) × 100
    assert margin == D("4600")

    ledger.hold_margin(margin)
    assert ledger.buying_power == D("95400")           # BP strained by the open entry
    ledger.release_margin(margin)                       # released on close
    assert ledger.buying_power == ledger.cash == D("100000")


def test_tc_sim_03_insufficient_bp_skips_entry_rejected_bp():
    poor = SimLedger(cash=D("1000"))                    # cannot afford a 4600 margin
    b = SimulatedBroker(poor, fee_per_leg=D("0.65"))
    b.set_market(_fill_credit)
    asyncio.run(b.submit(condor_intent("4.00")))
    assert poor.cash == D("1000")                       # never filled — cash untouched
    assert any(isinstance(e, dict) and e.get("reason") == "rejected_bp" for e in b.events)

    # with capital, the same entry fills AND holds the margin against BP
    rich = SimLedger(cash=D("100000"))
    b2 = SimulatedBroker(rich, fee_per_leg=D("0"))
    b2.set_market(_fill_credit)
    asyncio.run(b2.submit(condor_intent("4.00")))
    assert rich.buying_power == D("100000") + D("400") - D("4600")  # credit in, margin held


def test_tc_sim_03_settlement_posts_against_closing_level():
    # 0DTE cash settlement against the real SPX close: a side that settles 5pts
    # ITM posts −5 × 100 to the ledger, exactly like real expiry.
    ledger = SimLedger(cash=D("100000"))
    ledger.post_fill(D("-5") * 100, fee=D("0"))
    assert ledger.cash == D("99500")


def test_tc_sim_03_ledger_survives_container_restart():
    ledger = SimLedger(cash=D("100397.40"))
    ledger.hold_margin(D("4600"))

    snapshot = ledger.to_dict()                         # durable state (REC-07)
    restored = SimLedger.from_dict(snapshot)            # a fresh process rebuilds it

    assert restored.cash == D("100397.40")
    assert restored.buying_power == D("95797.40")       # cash and held margin both survive
