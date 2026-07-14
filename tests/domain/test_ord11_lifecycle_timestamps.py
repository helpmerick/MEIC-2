"""ORD-11 (v1.67): every stop/LEX lifecycle event carries an `at:` timestamp,
additive/optional (default None) so the live journal's 3900+ existing events
still replay IDENTICALLY -- watchdogs must never have to track wall-clock
first-sighting themselves (a restart resets that clock, the 07-13 review
finding this rule exists for).

Covers the twelve event classes ORD-11 names: StopPlaced, StopReplaced,
StopConfirmed, ShortStopped, LongSaleStarted, LongSaleRepriced, LongSold,
SideClosed, SideExpired, EntryClosed, LexOrderPlaced, WatchdogEscalated.
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from meic.domain.events import (
    EntryClosed,
    Event,
    LexOrderPlaced,
    LongSaleRepriced,
    LongSaleStarted,
    LongSold,
    ShortStopped,
    SideClosed,
    SideExpired,
    StopConfirmed,
    StopPlaced,
    StopReplaced,
    WatchdogEscalated,
)
from meic.domain.projection import fold

AT = "2026-07-14T09:00:00+00:00"

# (event class, required-field kwargs) -- minimal valid construction of each.
_CASES = [
    (StopPlaced, dict(entry_id="d#1", side="PUT", trigger=D("3.80"))),
    (StopReplaced, dict(entry_id="d#1", side="PUT")),
    (StopConfirmed, dict(entry_id="d#1", side="PUT")),
    (ShortStopped, dict(entry_id="d#1", side="PUT", fill=D("3.85"), slippage=D("0.05"))),
    (LongSaleStarted, dict(entry_id="d#1", side="PUT")),
    (LongSaleRepriced, dict(entry_id="d#1", side="PUT", step=1, price=D("0.40"))),
    (LongSold, dict(entry_id="d#1", side="PUT", recovery=D("0.40"))),
    (SideClosed, dict(entry_id="d#1", side="PUT")),
    (SideExpired, dict(entry_id="d#1", side="PUT")),
    (EntryClosed, dict(entry_id="d#1", initiator="eod")),
    (LexOrderPlaced, dict(entry_id="d#1", side="PUT", broker_order_id="1", price=D("0.40"),
                          kind="ladder")),
    (WatchdogEscalated, dict(entry_id="d#1", side="PUT", mark_at_breach=D("3.80"),
                             elapsed_seconds=D("20"), fill_price=D("3.85"))),
]


@pytest.mark.parametrize("cls,kwargs", _CASES, ids=[c.__name__ for c, _ in _CASES])
def test_at_defaults_to_none(cls, kwargs):
    """Additive/optional: a construction call from BEFORE ORD-11 (no `at`
    kwarg threaded through yet) must still work, defaulting to None."""
    event = cls(**kwargs)
    assert event.at is None


@pytest.mark.parametrize("cls,kwargs", _CASES, ids=[c.__name__ for c, _ in _CASES])
def test_at_round_trips_through_to_dict_from_dict(cls, kwargs):
    event = cls(at=AT, **kwargs)
    restored = Event.from_dict(event.to_dict())
    assert restored == event
    assert restored.at == AT


@pytest.mark.parametrize("cls,kwargs", _CASES, ids=[c.__name__ for c, _ in _CASES])
def test_legacy_dict_with_no_at_key_still_replays(cls, kwargs):
    """The pin ORD-11 exists to guarantee: an event serialized BEFORE this
    change (its dict has no "at" key at all -- the real shape of every
    pre-v1.67 journal row) must still deserialize, via `Event.from_dict`'s
    generic "field absent -> use the dataclass default" path, and equal the
    same event constructed with `at=None` explicitly."""
    event = cls(**kwargs)  # at=None (the default)
    legacy_dict = event.to_dict()
    del legacy_dict["at"]  # simulate a row written before `at` existed at all

    restored = Event.from_dict(legacy_dict)
    assert restored == event
    assert restored.at is None


def test_legacy_log_folds_identically_with_or_without_at_populated():
    """A day's log built with real `at` timestamps (as every ORD-11-era
    caller now populates) folds to the SAME `DayState` as the identical log
    with every `at` stripped to None (as every pre-ORD-11 caller recorded) --
    `at` is pure metadata, never consumed by the money projection."""
    from meic.domain.events import CondorFilled

    def _log(at):
        return [
            CondorFilled(entry_id="2026-07-10#1", net_credit=D("3.60"), fee=D("0")),
            StopPlaced(entry_id="2026-07-10#1", side="CALL", trigger=D("3.80"), at=at),
            ShortStopped(entry_id="2026-07-10#1", side="CALL", fill=D("3.85"),
                        slippage=D("0.05"), at=at),
            LongSold(entry_id="2026-07-10#1", side="CALL", recovery=D("0.40"), at=at),
            SideClosed(entry_id="2026-07-10#1", side="CALL", at=at),
            EntryClosed(entry_id="2026-07-10#1", initiator="eod", at=at),
        ]

    with_timestamps = fold(_log(AT))
    without_timestamps = fold(_log(None))
    assert with_timestamps == without_timestamps
    assert str(with_timestamps.day_pnl) == str(without_timestamps.day_pnl)
