"""reporting.mae_mfe — RPT-12 D8/D10 MAE from recorded EntryMarkSamples only."""
from decimal import Decimal as D

from meic.domain.events import EntryMarkSample
from meic.reporting.mae_mfe import excursion


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
