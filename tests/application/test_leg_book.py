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
