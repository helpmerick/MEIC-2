"""UC-10 / DAY-05 trading-mode promotion: flat-book + typed-LIVE gate, staged
for next day, applied at boot from the durable log (TC-UI-04 as a real service)."""
import asyncio
from datetime import datetime
from decimal import Decimal as D

from meic.application.mode_switch import (
    apply_pending_mode,
    pending_mode,
    request_mode_switch,
)
from meic.composition.paper import PaperComposition
from meic.composition.panel_commands import PanelCommands
from meic.domain.events import CondorFilled, ModeSwitchStaged
from meic.domain.ticks import TickRung, TickTable
from tests.harness.fake_clock import ET, FakeClock

SPX = TickTable((TickRung(D("3.00"), D("0.05")), TickRung(None, D("0.10"))))


def _comp():
    return PaperComposition(clock=FakeClock(datetime(2026, 7, 7, 9, 30, tzinfo=ET)), ticks=SPX)


# --- pure decision (DAY-05 / UC-10) ------------------------------------------

def test_request_mode_switch_gates():
    flat = dict(open_positions=0, working_orders=0)
    # promote to live: flat book + typed LIVE -> staged, effective next day
    r = request_mode_switch(target="live", current="paper", confirmation="LIVE", **flat)
    assert r.staged and r.target == "live" and r.effective == "next_day"
    # missing/incorrect confirmation -> rejected
    assert request_mode_switch(target="live", current="paper", confirmation="", **flat).reason == "confirmation_required"
    # book not flat -> rejected regardless of confirmation
    assert request_mode_switch(target="live", current="paper", confirmation="LIVE",
                               open_positions=1, working_orders=0).reason == "book_not_flat"
    # switch back to paper: no token needed, still flat + next-day
    assert request_mode_switch(target="paper", current="live", **flat).staged is True
    # already in mode / unknown mode
    assert request_mode_switch(target="paper", current="paper", **flat).reason == "already_in_mode"
    assert request_mode_switch(target="bogus", current="paper", **flat).reason == "unknown_mode"


# --- next-day application from the durable log -------------------------------

def test_pending_and_apply_next_day():
    events = [ModeSwitchStaged(target="live", effective="next_day")]
    assert pending_mode(events) == "live"

    comp = _comp()  # starts paper
    comp.events.extend(events)
    assert comp.state.trading_mode == "paper"
    applied = apply_pending_mode(comp.state, comp.events)     # at next-day boot
    assert applied == "live" and comp.state.trading_mode == "live"


# --- PanelCommands.switch_mode against the live book -------------------------

def test_switch_mode_rejects_open_book():
    comp = _comp()
    comp.events.append(CondorFilled(entry_id="e1", net_credit=D("4.00")))  # open position
    rej = asyncio.run(PanelCommands(comp).switch_mode("live", "LIVE"))
    assert rej == {"staged": False, "target": "live", "effective": "next_day", "reason": "book_not_flat"}
    assert not any(isinstance(e, ModeSwitchStaged) for e in comp.events)


def test_switch_mode_stages_when_flat_but_does_not_apply_intraday():
    comp = _comp()  # fresh: no entries, no working orders -> flat book
    cmd = PanelCommands(comp)

    # a typed LIVE on a flat book stages the switch (effective next day)
    ok = asyncio.run(cmd.switch_mode("live", "LIVE"))
    assert ok["staged"] is True and ok["target"] == "live" and ok["effective"] == "next_day"
    assert any(isinstance(e, ModeSwitchStaged) and e.target == "live" for e in comp.events)
    assert comp.state.trading_mode == "paper"  # DAY-05: NOT applied intraday

    # without the typed LIVE it is refused
    comp2 = _comp()
    refused = asyncio.run(PanelCommands(comp2).switch_mode("live", ""))
    assert refused["staged"] is False and refused["reason"] == "confirmation_required"
