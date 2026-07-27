"""ENT-11(1) terminal sweep — settles what is provably finished, touches
nothing else. Born from marathon day-1's catch: a both-sides-stopped entry
left its armed floor silently inert forever; the expired phantoms
(2026-07-14#101 here, 2026-07-20#7 on Rick's install) are the same class.
"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal as D

from meic.application.terminal_sweep import sweep_terminals
from meic.application.terminal_state import TerminalStateResolver
from meic.domain.events import CondorFilled, EntryClosed, FilledLeg, ShortStopped

TODAY = date(2026, 7, 27)


def _legs(ymd="260727"):
    return (FilledLeg(f"SPXW  {ymd}P07330000", "P", "long", 1),
            FilledLeg(f"SPXW  {ymd}P07380000", "P", "short", 1),
            FilledLeg(f"SPXW  {ymd}C07410000", "C", "short", 1),
            FilledLeg(f"SPXW  {ymd}C07460000", "C", "long", 1))


class _Broker:
    def __init__(self, rows=(), raises=None):
        self.rows, self.raises = list(rows), raises

    async def positions(self):
        if self.raises:
            raise self.raises
        return self.rows


class _Row:
    def __init__(self, symbol):
        self.symbol = symbol
        self.instrument_type = "Equity Option"
        self.quantity = D("1")
        self.quantity_direction = "Short"
        self.restricted_quantity = D("0")


def _stopped_both(entry_id="e1"):
    return [CondorFilled(entry_id=entry_id, net_credit=D("34.35"), legs=_legs()),
            ShortStopped(entry_id=entry_id, side="PUT", fill=D("6"), slippage=D("0"),
                         initiator="resting_stop"),
            ShortStopped(entry_id=entry_id, side="CALL", fill=D("6"), slippage=D("0"),
                         initiator="resting_stop")]


def _sweep(events, broker):
    return asyncio.run(sweep_terminals(events, TerminalStateResolver(broker), today=TODAY))


def test_sweep_settles_a_both_sides_stopped_entry_the_broker_confirms_flat():
    """THE DAY-1 CATCH: journal says every side is done, broker says flat ->
    the resolver's verdict journals the terminal, the floor's entry is closed,
    and the silent armed floor can no longer exist."""
    events = _stopped_both()
    assert _sweep(events, _Broker([])) == ["e1"]
    closes = [e for e in events if isinstance(e, EntryClosed)]
    assert len(closes) == 1 and closes[0].initiator == "settled"


def test_sweep_settles_an_expired_phantom():
    """The 2026-07-14#101 / Rick's #7 class: expired legs, no closing event,
    no broker position left to diff -- date gate admits it, resolver settles."""
    events = [CondorFilled(entry_id="ph", net_credit=D("3.60"), legs=_legs("260714"))]
    assert _sweep(events, _Broker([])) == ["ph"]


def test_sweep_NEVER_touches_a_live_entry_even_when_positions_lags():
    """THE SAFETY GATE. A freshly-filled same-day entry with NO stops and NO
    expiry passed is not even a candidate -- however flat a lagging
    positions() read claims it is. Without this, the sweep could journal a
    LIVE condor closed: the 2026-07-20 incident class from a new direction."""
    events = [CondorFilled(entry_id="live", net_credit=D("3.60"), legs=_legs())]
    assert _sweep(events, _Broker([])) == []          # broker (wrongly) says flat
    assert not any(isinstance(e, EntryClosed) for e in events)


def test_sweep_refuses_on_UNKNOWN_and_on_a_still_held_leg():
    """ENT-11(2): UNKNOWN never settles -- the entry stays visible. And a leg
    the broker still HOLDS (LEX long not yet sold) blocks settlement too."""
    down = _stopped_both("u1")
    assert _sweep(down, _Broker(raises=ConnectionError("down"))) == []
    held = _stopped_both("u2")
    assert _sweep(held, _Broker([_Row("SPXW  260727P07330000")])) == []
    assert not any(isinstance(e, EntryClosed) for e in down + held)


def test_sweep_is_idempotent():
    events = _stopped_both()
    broker = _Broker([])
    assert _sweep(events, broker) == ["e1"]
    assert _sweep(events, broker) == []               # already terminal -> skipped
    assert sum(isinstance(e, EntryClosed) for e in events) == 1


def test_an_unparsable_symbol_never_reads_as_expired():
    events = [CondorFilled(entry_id="x", net_credit=D("1"),
                           legs=(FilledLeg("WEIRD", "P", "short", 1),))]
    assert _sweep(events, _Broker([])) == []
