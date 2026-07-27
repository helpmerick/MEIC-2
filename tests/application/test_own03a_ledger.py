"""OWN-03a — provable ownership: the ledger is derived from the journal.

THE DEFECT: `apply_fill` had ZERO production call sites, so every ledger was
empty and every own leg classified FOREIGN — the desync that made an external
operator's bot quarantine NINE of its own legs. The fix is ownership PROVEN
from ORD-10's journaled evidence and restored per REC-07 — never asserted ad
hoc, never auto-adopted from broker positions (OWN-03's line, unweakened).
"""
from __future__ import annotations

from decimal import Decimal as D

from meic.composition.paper import PaperComposition
from meic.domain.events import (
    CondorFilled,
    EntryClosed,
    FilledLeg,
    ShortStopped,
    SideExpired,
)
from meic.domain.ownership import OwnershipLedger
from tests.application.test_compositions import CLOCK, SPX

P_LONG, P_SHORT = "SPXW  260727P07385000", "SPXW  260727P07435000"
C_SHORT, C_LONG = "SPXW  260727C07505000", "SPXW  260727C07555000"


def _legs():
    return (FilledLeg(P_LONG, "P", "long", 1), FilledLeg(P_SHORT, "P", "short", 1),
            FilledLeg(C_SHORT, "C", "short", 1), FilledLeg(C_LONG, "C", "long", 1))


def _filled(entry_id="e1"):
    return CondorFilled(entry_id=entry_id, net_credit=D("3.60"), legs=_legs())


def test_own03a_the_composition_ledger_and_close_entrys_are_the_same_object():
    """The identity proof is load-bearing (NFR-11): a CloseEntry holding a
    DIFFERENT (empty) ledger would resurrect the zero-call-sites defect
    invisibly — present, tested, and never consulted."""
    comp = PaperComposition(clock=CLOCK, ticks=SPX)
    assert isinstance(comp.ledger, OwnershipLedger)
    assert comp.close._ledger is comp.ledger


def test_own03a_a_journaled_fill_is_owned_with_broker_reported_signs():
    ledger = OwnershipLedger.from_events([_filled()])
    assert ledger.owned(P_LONG) == 1 and ledger.owned(C_LONG) == 1
    assert ledger.owned(P_SHORT) == -1 and ledger.owned(C_SHORT) == -1


def test_own03a_restart_restores_ownership_from_the_journal():
    """REC-07: a rebuilt composition derives the SAME ownership from the same
    journal — no auto-re-adoption from broker positions, ever."""
    events = [_filled()]
    comp = PaperComposition(clock=CLOCK, ticks=SPX, events=events)
    assert comp.ledger.owned(P_SHORT) == -1


def test_own03a_a_stopped_side_contributes_nothing_understating_by_design():
    """The short was bought back; whether LEX sold the long is NOT derivable
    (recoveries is a sum, not per-side), so the stopped side contributes
    NOTHING. Understating is the contained direction: cap_exit_qty's zero
    falls through to the caller's quantity and ORD-12's resolver (broker
    positions) is the real gate. Overstating would let the cap fail to bite."""
    ledger = OwnershipLedger.from_events([
        _filled(),
        ShortStopped(entry_id="e1", side="PUT", fill=D("3.80"),
                     slippage=D("0"), initiator="resting_stop"),
    ])
    assert ledger.owned(P_SHORT) == 0 and ledger.owned(P_LONG) == 0
    assert ledger.owned(C_SHORT) == -1 and ledger.owned(C_LONG) == 1


def test_own03a_expiry_and_entry_close_write_the_legs_down():
    assert OwnershipLedger.from_events([
        _filled(), SideExpired(entry_id="e1", side="PUT"),
    ]).owned(P_SHORT) == 0
    ledger = OwnershipLedger.from_events([
        _filled(), EntryClosed(entry_id="e1", initiator="manual"),
    ])
    for sym in (P_LONG, P_SHORT, C_SHORT, C_LONG):
        assert ledger.owned(sym) == 0


def test_own03a_refresh_is_in_place_never_a_rebind():
    """NFR-11: holders captured THIS object, so the refresh mutates it."""
    events = [_filled()]
    comp = PaperComposition(clock=CLOCK, ticks=SPX, events=events)
    before = comp.ledger
    comp.events.append(_filled("e2"))
    comp.ledger.refresh_from_events(comp.events)
    assert comp.ledger is before
    assert comp.ledger.owned(P_SHORT) == -2      # both entries' shorts


def test_own03a_operator_trades_never_enter_the_ledger():
    """OWN-01: the ledger is fed ONLY by journaled bot fills. A broker
    position with no journal evidence stays FOREIGN — quarantined, alerted,
    never adopted."""
    ledger = OwnershipLedger.from_events([_filled()])
    assert ledger.owned("SPXW  261231C08010000") == 0   # the operator's own spread
    assert ledger.classify("SPXW  261231C08010000", -1).name == "FOREIGN"
