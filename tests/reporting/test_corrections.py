"""reporting.corrections — RPT-15 broker-truth override, never silent."""
from decimal import Decimal as D

from meic.domain.events import CorrectionRecord
from meic.reporting.corrections import corrected_value, corrections_for_day


def test_no_correction_renders_the_plain_fold_value():
    assert corrected_value([], "2026-07-09", "fees", D("220.00")) == D("220.00")


def test_a_correction_overrides_with_broker_truth():
    # scope="own": only an own-scoped record (OWN-01/OWN-03) may override --
    # see test_a_legacy_correction_with_no_scope_never_overrides below for the
    # legacy (scope=None) case, which must NOT override.
    events = [CorrectionRecord(date="2026-07-09", field="fees", bot_value="220.00",
                               broker_value="240.00", diff="20.00", at="t", scope="own")]
    assert corrected_value(events, "2026-07-09", "fees", D("220.00")) == D("240.00")


def test_a_correction_for_a_different_field_or_day_never_leaks():
    events = [CorrectionRecord(date="2026-07-09", field="cash_delta", bot_value="1",
                               broker_value="2", diff="1", at="t")]
    assert corrected_value(events, "2026-07-09", "fees", D("220.00")) == D("220.00")
    assert corrected_value(events, "2026-07-08", "cash_delta", D("1")) == D("1")


def test_a_legacy_correction_with_no_scope_never_overrides():
    """THE SAFETY TEST. A record with no `scope` (scope=None) predates the
    2026-07-12 OWN-01/OWN-03 own-scoping fix and may carry a whole-shared-
    account-polluted `broker_value` (the real 2026-07-10 incident record
    claims cash_delta -534.46 for a day the bot's own trade actually made
    +43.68). Such a record must NEVER override the fold value -- it must
    render as if it were not there at all."""
    events = [CorrectionRecord(date="2026-07-10", field="cash_delta", bot_value="40.00",
                               broker_value="-534.46", diff="-574.46", at="t")]
    assert corrected_value(events, "2026-07-10", "cash_delta", D("40.00")) == D("40.00")


def test_an_own_scoped_correction_does_override():
    events = [CorrectionRecord(date="2026-07-10", field="cash_delta", bot_value="40.00",
                               broker_value="43.68", diff="3.68", at="t", scope="own")]
    assert corrected_value(events, "2026-07-10", "cash_delta", D("40.00")) == D("43.68")


def test_the_newest_own_scoped_correction_for_a_day_field_wins():
    """OWN-01 append-only retraction (2026-07-14): the log is append-only, so
    a stale own-scoped record (e.g. one computed before an
    `OwnOrderIdRetracted` withdrew a mistakenly-claimed order id) is NEVER
    removed -- a fresh reconcile simply appends a NEW own-scoped record for
    the same (day, field). The newest one must be the one rendered, not the
    oldest and not an arbitrary one."""
    events = [
        CorrectionRecord(date="2026-07-10", field="cash_delta", bot_value="40.00",
                         broker_value="43.68", diff="3.68", at="t1", scope="own"),
        CorrectionRecord(date="2026-07-10", field="cash_delta", bot_value="40.00",
                         broker_value="34.40", diff="-5.60", at="t2", scope="own"),
    ]
    assert corrected_value(events, "2026-07-10", "cash_delta", D("40.00")) == D("34.40")


def test_corrections_for_day_returns_only_that_day():
    events = [
        CorrectionRecord(date="2026-07-08", field="fees", bot_value="1",
                         broker_value="2", diff="1", at="t"),
        CorrectionRecord(date="2026-07-09", field="fees", bot_value="3",
                         broker_value="4", diff="1", at="t"),
    ]
    got = corrections_for_day(events, "2026-07-09")
    assert len(got) == 1 and got[0].bot_value == "3"
