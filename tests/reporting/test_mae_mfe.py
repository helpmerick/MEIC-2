"""reporting.mae_mfe — RPT-12 D8/D10 MAE from recorded EntryMarkSamples only."""
from decimal import Decimal as D

from meic.domain.events import EntryMarkSample
from meic.reporting.mae_mfe import consumed_fraction, excursion


def _sample(entry_id, mid, side="PUT"):
    field = "put_short_mid" if side == "PUT" else "call_short_mid"
    return EntryMarkSample(entry_id=entry_id, at="t", **{field: mid})


def test_mae_pct_and_survived_for_a_peak_short_of_the_trigger():
    samples = [_sample("e1", D("3.20")), _sample("e1", D("3.60")), _sample("e1", D("3.10"))]
    result = excursion("e1", "PUT", samples, fill=D("3.00"), trigger=D("3.80"))
    assert result.mae_pct == D("0.75")
    assert result.survived is True


def test_a_peak_at_or_past_the_trigger_never_survives():
    samples = [_sample("e1", D("3.80"))]
    result = excursion("e1", "PUT", samples, fill=D("3.00"), trigger=D("3.80"))
    assert result.mae_pct == D("1")
    assert result.survived is False


def test_no_recorded_sample_for_the_entry_side_is_a_gap_not_a_guess():
    samples = [_sample("e1", D("3.60"))]
    assert excursion("e2", "PUT", samples, fill=D("3.00"), trigger=D("3.80")) is None
    assert excursion("e1", "CALL", samples, fill=D("3.00"), trigger=D("3.80")) is None


def test_none_valued_samples_are_excluded_never_treated_as_zero():
    samples = [EntryMarkSample(entry_id="e1", at="t", put_short_mid=None),
               _sample("e1", D("3.40"))]
    result = excursion("e1", "PUT", samples, fill=D("3.00"), trigger=D("3.80"))
    assert result.mae_pct == D("0.5")  # only the 3.40 sample counted, not a fabricated 0


# --- consumed_fraction: the shared formula (operator ruling 2026-07-11) -------
# `excursion` above and application/drills.py's near-trigger drill guidance
# MUST share this ONE implementation — never two copies of the same math.

def test_consumed_fraction_matches_excursions_own_arithmetic():
    samples = [_sample("e1", D("3.80"))]
    result = excursion("e1", "PUT", samples, fill=D("3.00"), trigger=D("3.80"))
    assert consumed_fraction(D("3.80"), fill=D("3.00"), trigger=D("3.80")) == result.mae_pct == D("1")


def test_consumed_fraction_is_none_when_trigger_equals_fill():
    """No distance to measure against — D10-style honesty, never a fabricated
    zero or infinity."""
    assert consumed_fraction(D("3.50"), fill=D("3.00"), trigger=D("3.00")) is None
