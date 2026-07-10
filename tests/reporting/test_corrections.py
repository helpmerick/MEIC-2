"""reporting.corrections — RPT-15 broker-truth override, never silent."""
from decimal import Decimal as D

from meic.domain.events import CorrectionRecord
from meic.reporting.corrections import corrected_value, corrections_for_day


def test_no_correction_renders_the_plain_fold_value():
    assert corrected_value([], "2026-07-09", "fees", D("220.00")) == D("220.00")


def test_a_correction_overrides_with_broker_truth():
    events = [CorrectionRecord(date="2026-07-09", field="fees", bot_value="220.00",
                               broker_value="240.00", diff="20.00", at="t")]
    assert corrected_value(events, "2026-07-09", "fees", D("220.00")) == D("240.00")


def test_a_correction_for_a_different_field_or_day_never_leaks():
    events = [CorrectionRecord(date="2026-07-09", field="cash_delta", bot_value="1",
                               broker_value="2", diff="1", at="t")]
    assert corrected_value(events, "2026-07-09", "fees", D("220.00")) == D("220.00")
    assert corrected_value(events, "2026-07-08", "cash_delta", D("1")) == D("1")


def test_corrections_for_day_returns_only_that_day():
    events = [
        CorrectionRecord(date="2026-07-08", field="fees", bot_value="1",
                         broker_value="2", diff="1", at="t"),
        CorrectionRecord(date="2026-07-09", field="fees", bot_value="3",
                         broker_value="4", diff="1", at="t"),
    ]
    got = corrections_for_day(events, "2026-07-09")
    assert len(got) == 1 and got[0].bot_value == "3"
