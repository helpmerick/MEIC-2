"""Terminal sweep — the resolver's boot/periodic caller (ENT-11(1)).

THE CLASS THIS ENDS (marathon day 1, 2026-07-27): entries that finish without
a closing event linger open forever. Two shapes, one cause:
  * both sides stopped + LEX-recovered -> flat at the broker, floor still
    armed and silently inert (cert entry 2026-07-27#3, reproduced offline);
  * expired-but-never-closed phantoms (our 2026-07-14#101, Rick's
    2026-07-20#7) -- no broker position remains, so position-diff reconcile
    has nothing to diff and misses them silently.

ENT-11(1): every path that decides an entry is finished calls THE resolver,
and only the resolver's verdict journals a terminal. This module is exactly
that caller -- it contains NO inference of its own.

SAFETY GATE, non-negotiable: an entry is a CANDIDATE only when the JOURNAL
already says it is done -- every side stopped/closed/expired -- or its
expiration date has passed. A freshly-filled entry with a lagging positions()
read is therefore never even considered; without this gate the sweep could
journal a live condor closed, which is the 2026-07-20 incident class from a
new direction. Broker truth then CONFIRMS: only a positive
TERMINAL_NO_POSITION on EVERY leg settles; any UNKNOWN leaves the entry
visible and untouched (ENT-11(2)).
"""
from __future__ import annotations

from datetime import date

from meic.application.terminal_state import LegState
from meic.domain.events import EntryClosed
from meic.domain.projection import fold

_TERMINAL = {"CLOSED", "EXPIRED", "DECAY_CLOSED"}


def _expired(entry, today: date | None) -> bool:
    if today is None or not entry.legs:
        return False
    try:
        sym = entry.legs[0].symbol          # OCC-21: root(6) YYMMDD ...
        y, m, d = 2000 + int(sym[6:8]), int(sym[8:10]), int(sym[10:12])
        return date(y, m, d) < today
    except (ValueError, IndexError):
        return False                        # unparsable -> NOT expired (fail closed)


def _journal_resolved(entry) -> bool:
    gone = set(entry.sides_stopped) | set(entry.sides_closed) | set(entry.sides_expired)
    sides = {leg.side for leg in entry.legs}
    return bool(sides) and sides <= gone


async def sweep_terminals(events, resolver, *, today: date | None = None,
                          alerts=None, at: str | None = None) -> list[str]:
    """Settle every provably-finished entry. Returns the settled ids."""
    settled: list[str] = []
    for entry_id, e in fold(events).entries.items():
        if e.close_initiator is not None or e.status in _TERMINAL:
            continue
        if not (_journal_resolved(e) or _expired(e, today)):
            continue                        # the SAFETY GATE -- see module docstring
        symbols = [leg.symbol for leg in e.legs]
        state, _ = await resolver.resolve_entry(symbols)
        if state is not LegState.TERMINAL_NO_POSITION:
            continue                        # UNKNOWN or held: stays visible, untouched
        events.append(EntryClosed(entry_id=entry_id, initiator="settled", at=at))
        settled.append(entry_id)
        if alerts is not None:
            alerts.alert("info", f"terminal sweep: {entry_id} settled -- journal showed it "
                                 "finished and the broker confirms flat on every leg (ENT-11)",
                         entry_id=entry_id)
    return settled
