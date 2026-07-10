"""reporting.trust — UI-25 trust stamps from RPT-15 events."""
from meic.domain.events import CorrectionRecord, DayBrokerConfirmed
from meic.reporting.trust import trust_stamp


def test_all_days_confirmed_is_broker_confirmed():
    events = [DayBrokerConfirmed(date="2026-07-08", at="t"),
              DayBrokerConfirmed(date="2026-07-09", at="t")]
    trust = trust_stamp(events, ("2026-07-08", "2026-07-09"))
    assert trust.status == "broker-confirmed"
    assert trust.label == "broker-confirmed"


def test_partial_confirmation_reports_the_count():
    events = [DayBrokerConfirmed(date="2026-07-08", at="t")]
    trust = trust_stamp(events, ("2026-07-08", "2026-07-09"))
    assert trust.status == "bot-computed"
    assert trust.label == "1/2 days broker-confirmed"


def test_a_corrected_day_counts_as_reconciled_not_unreconciled():
    """A CorrectionRecord means RPT-15 ran and resolved the day (to broker
    truth) -- it is not the same as "never reconciled" (broker unreachable)."""
    events = [CorrectionRecord(date="2026-07-08", field="fees", bot_value="1",
                               broker_value="2", diff="1", at="t")]
    trust = trust_stamp(events, ("2026-07-08",))
    assert trust.status == "broker-confirmed"
    assert trust.confirmed_days == 1


def test_no_days_in_scope_is_bot_computed_not_a_division_error():
    trust = trust_stamp([], ())
    assert trust.status == "bot-computed"
    assert trust.confirmed_days == 0 and trust.total_days == 0
