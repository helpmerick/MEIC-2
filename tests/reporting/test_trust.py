"""reporting.trust — UI-25 trust stamps from RPT-15 events."""
from meic.domain.events import CorrectionRecord, DayBrokerConfirmed, ExternalFillImported
from meic.reporting.trust import trust_stamp


def _imported(day: str) -> ExternalFillImported:
    return ExternalFillImported(
        day=day, at=f"{day}T14:00:00-04:00", order_id="482214732",
        symbol="SPXW  260709P05600000", action="Sell to Open", quantity=1,
        price=None, fee=None, imported_at=f"{day}T09:00:00-04:00",
        source="tastytrade_history")


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


def test_a_fully_imported_scope_is_broker_imported_not_broker_confirmed():
    """RPT-16(4): an imported day's numbers are broker truth by construction,
    but it must never be labeled broker-confirmed -- that label means the
    bot's OWN computation matched, which never happened here."""
    trust = trust_stamp([_imported("2026-07-09")], ("2026-07-09",))
    assert trust.status == "broker-imported"
    assert trust.label == "broker-imported"
    assert trust.imported_days == 1


def test_an_imported_day_is_never_broker_confirmed_even_with_a_stray_confirmation():
    events = [_imported("2026-07-09"), DayBrokerConfirmed(date="2026-07-09", at="t")]
    trust = trust_stamp(events, ("2026-07-09",))
    assert trust.status == "broker-imported"
    assert trust.confirmed_days == 0


def test_a_mixed_scope_counts_imported_days_out_separately():
    events = [DayBrokerConfirmed(date="2026-07-08", at="t"), _imported("2026-07-09")]
    trust = trust_stamp(events, ("2026-07-08", "2026-07-09"))
    assert trust.status == "bot-computed"
    assert trust.imported_days == 1
    assert trust.label == "1/2 days broker-confirmed · 1 imported day"
