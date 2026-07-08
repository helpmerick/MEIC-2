"""LegBook — ORD-09: every post-fill action reads leg identity from the log."""
from datetime import date
from decimal import Decimal as D

from meic.application.leg_book import LegBook, crosscheck_leg_symbols
from meic.domain.events import CondorFilled, EntryClosed, FilledLeg

EXP = date(2026, 7, 6)
PUT_LONG = "SPXW  260706P05940000"
PUT_SHORT = "SPXW  260706P05990000"
CALL_SHORT = "SPXW  260706C06060000"
CALL_LONG = "SPXW  260706C06110000"

LEGS = (FilledLeg(PUT_LONG, "P", "long", 2),
        FilledLeg(PUT_SHORT, "P", "short", 2),
        FilledLeg(CALL_SHORT, "C", "short", 2),
        FilledLeg(CALL_LONG, "C", "long", 2))


def _events():
    return [CondorFilled(entry_id="d#1", net_credit=D("4.00"), legs=LEGS),
            CondorFilled(entry_id="d#2", net_credit=D("3.00"), legs=LEGS[:2]),
            EntryClosed(entry_id="d#2", initiator="manual")]


def test_the_book_indexes_legs_by_entry():
    book = LegBook.from_events(_events())
    assert len(book.of("d#1")) == 4 and len(book.of("d#2")) == 2
    assert book.of("nope") == ()


def test_symbol_lookup_by_side_and_role():
    book = LegBook.from_events(_events())
    assert book.symbol("d#1", "PUT", "short") == PUT_SHORT
    assert book.symbol("d#1", "PUT", "long") == PUT_LONG
    assert book.symbol("d#1", "CALL", "short") == CALL_SHORT
    assert book.symbol("d#1", "CALL", "long") == CALL_LONG
    assert book.symbol("d#1", "CALL", "wings") is None


def test_shorts_and_open_sides():
    book = LegBook.from_events(_events())
    assert {l.symbol for l in book.shorts("d#1")} == {PUT_SHORT, CALL_SHORT}
    assert book.open_sides("d#1") == ("CALL", "PUT")
    assert book.open_sides("d#2") == ("PUT",)


def test_a_fill_with_no_recorded_legs_is_absent_not_guessed():
    """A caller must be able to tell "no legs recorded" from "legs recorded".
    Inventing a symbol here is precisely the defect ORD-09 closes."""
    book = LegBook.from_events([CondorFilled(entry_id="d#1", net_credit=D("4.00"))])
    assert book.of("d#1") == () and book.symbol("d#1", "PUT") is None


def test_the_book_carries_the_quantity_each_leg_filled():
    """STP-01: the stop is sized from the leg's FILLED quantity."""
    assert all(l.qty == 2 for l in LegBook.from_events(_events()).shorts("d#1"))


# --- reconstruction is a cross-check, never a source ------------------------------

def test_crosscheck_passes_when_the_broker_agrees():
    assert crosscheck_leg_symbols(
        LEGS, underlying="SPXW", expiration=EXP,
        strikes={("P", "long"): D("5940"), ("P", "short"): D("5990"),
                 ("C", "short"): D("6060"), ("C", "long"): D("6110")}) == []


def test_crosscheck_names_both_values_on_a_mismatch():
    drifted = (FilledLeg("SPXW  260706P05995000", "P", "short", 2),)
    problems = crosscheck_leg_symbols(drifted, underlying="SPXW", expiration=EXP,
                                      strikes={("P", "short"): D("5990")})
    assert len(problems) == 1
    assert "P05995000" in problems[0] and "P05990000" in problems[0]
    assert "short PUT" in problems[0]


def test_crosscheck_skips_legs_it_has_no_strike_for():
    assert crosscheck_leg_symbols(LEGS, underlying="SPXW", expiration=EXP, strikes={}) == []


def test_crosscheck_reports_rather_than_raises_on_an_unrepresentable_strike():
    problems = crosscheck_leg_symbols((FilledLeg(PUT_SHORT, "P", "short", 1),),
                                      underlying="SPXW", expiration=EXP,
                                      strikes={("P", "short"): D("5990.00001")})
    assert len(problems) == 1 and "cannot reconstruct" in problems[0]


# --- ORD-09 hard refusal (v1.46, operator-ratified) ---------------------------

def test_protect_position_refuses_a_short_with_no_broker_symbol():
    """A stop must name the instrument the BROKER filled. There is no strike
    fallback: reconstructing here is action-time symbology, which ORD-09
    prohibits, and a stop resting on an instrument the broker never filled
    protects nothing."""
    import pytest
    from meic.application.protect_position import LegsUnrecorded, ShortLeg

    with pytest.raises(LegsUnrecorded, match="no broker-reported symbol"):
        ShortLeg("PUT", D("3.00"), D("0.50"))                 # nothing at all

    with pytest.raises(LegsUnrecorded, match="ORD-09"):
        ShortLeg("PUT", D("3.00"), D("0.50"), symbol="")      # empty is not identity

    ok = ShortLeg("PUT", D("3.00"), D("0.50"), symbol=PUT_SHORT)
    assert ok.symbol == PUT_SHORT and ok.right == "P"


def test_short_leg_no_longer_accepts_a_strike():
    """The strike field is gone: there is nothing left to reconstruct from."""
    import pytest
    from meic.application.protect_position import ShortLeg

    with pytest.raises(TypeError):
        ShortLeg("PUT", D("3.00"), D("0.50"), strike=D("5990"))


def test_a_fill_with_unrecorded_legs_refuses_to_protect_rather_than_guess():
    """End to end: the composition raises LegsUnrecorded instead of inventing."""
    import pytest
    from meic.application.protect_position import LegsUnrecorded

    class _Comp:
        events = [CondorFilled(entry_id="d#1", net_credit=D("4.00"))]  # ORD-09 legs absent
        from meic.composition.paper import PaperComposition as _P
        _shorts = _P._shorts

    class _Condor:
        put_short_mid, call_short_mid = D("3.00"), D("2.00")

    with pytest.raises(LegsUnrecorded, match="expected 2"):
        _Comp()._shorts("d#1", _Condor())
