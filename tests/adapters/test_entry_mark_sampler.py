"""RPT-12/D8 — `_sample_marks_once`: one EntryMarkSample per OPEN entry per
health tick, from the SAME chain snapshot `_live_pnl_enricher` already reads
(no new subscription). Unit-tested against a fake ChainSnapshot, no DXLink,
no broker — mirrors tests/adapters/test_live_pnl_enricher.py's fixtures.
"""
from datetime import date, datetime, timezone
from decimal import Decimal as D

from meic.adapters.api.server import _sample_marks_once
from meic.adapters.dxlink.chain_snapshot import ChainSnapshot
from meic.adapters.occ import occ_symbol
from meic.domain.chain import ChainSide, Mark
from meic.domain.events import CondorFilled, EntryClosed, EntryMarkSample, FilledLeg

TAKEN_AT = datetime(2026, 7, 9, 14, 35, tzinfo=timezone.utc)
EXP = date(2026, 7, 9)


class _Comp:
    """Minimal stand-in for a composition: `_sample_marks_once` only touches
    `comp.events` (append + fold reads it as a plain list)."""

    def __init__(self, events=None):
        self.events = list(events or [])


def _leg(right, role, strike, qty=1):
    return FilledLeg(symbol=occ_symbol("SPXW", EXP, right, D(strike)), right=right,
                      role=role, qty=qty, price=D("1.00"))


FULL_LEGS = (
    _leg("P", "short", "7535"),
    _leg("P", "long", "7510"),
    _leg("C", "short", "7540"),
    _leg("C", "long", "7565"),
)


def _snapshot(put_marks, call_marks, *, spot=D("7538"), stale=False):
    return ChainSnapshot(
        spot=spot, expiration=EXP,
        put_side=ChainSide(strikes_toward_otm=tuple(sorted(put_marks, reverse=True)), marks=put_marks),
        call_side=ChainSide(strikes_toward_otm=tuple(sorted(call_marks)), marks=call_marks),
        put_band=(), call_band=(), symbols={}, taken_at=TAKEN_AT, stale=stale)


FULL_PUT_MARKS = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75")),   # mid 1.70
                  D("7510"): Mark(bid=D("0.07"), ask=D("0.09"))}   # mid 0.08
FULL_CALL_MARKS = {D("7540"): Mark(bid=D("1.90"), ask=D("2.00")),  # mid 1.95
                   D("7565"): Mark(bid=D("0.06"), ask=D("0.08"))}  # mid 0.07


def test_open_entry_with_full_marks_and_spot_appends_a_complete_sample():
    comp = _Comp([CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS)])
    snap = _snapshot(FULL_PUT_MARKS, FULL_CALL_MARKS)

    _sample_marks_once(comp, snap)

    samples = [e for e in comp.events if isinstance(e, EntryMarkSample)]
    assert len(samples) == 1
    s = samples[0]
    assert s.entry_id == "2026-07-09#1"
    assert s.at == TAKEN_AT.isoformat()
    assert s.spot == D("7538")
    assert s.put_short_mid == D("1.70") and s.put_long_mid == D("0.08")
    assert s.call_short_mid == D("1.95") and s.call_long_mid == D("0.07")


def test_missing_mark_renders_that_field_none_never_fabricated():
    comp = _Comp([CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS)])
    put_marks = {D("7535"): Mark(bid=D("1.65"), ask=D("1.75"))}   # 7510 UNMARKED
    snap = _snapshot(put_marks, FULL_CALL_MARKS)

    _sample_marks_once(comp, snap)

    s = [e for e in comp.events if isinstance(e, EntryMarkSample)][0]
    assert s.put_short_mid == D("1.70")
    assert s.put_long_mid is None            # honest gap, D10 -- never interpolated
    assert s.call_short_mid == D("1.95") and s.call_long_mid == D("0.07")


def test_no_open_entries_appends_nothing():
    comp = _Comp([])  # empty log -- no entries at all
    snap = _snapshot(FULL_PUT_MARKS, FULL_CALL_MARKS)

    _sample_marks_once(comp, snap)

    assert comp.events == []


def test_closed_entry_is_not_sampled():
    comp = _Comp([
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS),
        EntryClosed(entry_id="2026-07-09#1", initiator="manual"),
    ])
    snap = _snapshot(FULL_PUT_MARKS, FULL_CALL_MARKS)

    _sample_marks_once(comp, snap)

    assert [e for e in comp.events if isinstance(e, EntryMarkSample)] == []


def test_stale_snapshot_samples_nothing():
    comp = _Comp([CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS)])
    snap = _snapshot(FULL_PUT_MARKS, FULL_CALL_MARKS, stale=True)

    _sample_marks_once(comp, snap)

    assert [e for e in comp.events if isinstance(e, EntryMarkSample)] == []


def test_none_snapshot_samples_nothing():
    comp = _Comp([CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS)])

    _sample_marks_once(comp, None)

    assert [e for e in comp.events if isinstance(e, EntryMarkSample)] == []


def test_everything_absent_appends_no_all_none_sample():
    """An open entry whose legs are entirely unmarked AND whose snapshot spot
    is absent must not produce a fabricated all-None row."""
    comp = _Comp([CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS)])
    snap = _snapshot({}, {}, spot=None)  # no marks at all, no spot either

    _sample_marks_once(comp, snap)

    assert [e for e in comp.events if isinstance(e, EntryMarkSample)] == []


def test_two_open_entries_each_get_their_own_sample():
    legs_2 = (
        _leg("P", "short", "7530"), _leg("P", "long", "7505"),
        _leg("C", "short", "7545"), _leg("C", "long", "7570"),
    )
    comp = _Comp([
        CondorFilled(entry_id="2026-07-09#1", net_credit=D("3.60"), legs=FULL_LEGS),
        CondorFilled(entry_id="2026-07-09#2", net_credit=D("3.40"), legs=legs_2),
    ])
    put_marks = dict(FULL_PUT_MARKS)
    put_marks[D("7530")] = Mark(bid=D("1.50"), ask=D("1.60"))
    put_marks[D("7505")] = Mark(bid=D("0.05"), ask=D("0.07"))
    call_marks = dict(FULL_CALL_MARKS)
    call_marks[D("7545")] = Mark(bid=D("1.70"), ask=D("1.80"))
    call_marks[D("7570")] = Mark(bid=D("0.04"), ask=D("0.06"))
    snap = _snapshot(put_marks, call_marks)

    _sample_marks_once(comp, snap)

    samples = {e.entry_id: e for e in comp.events if isinstance(e, EntryMarkSample)}
    assert set(samples) == {"2026-07-09#1", "2026-07-09#2"}
    assert samples["2026-07-09#2"].put_short_mid == D("1.55")
