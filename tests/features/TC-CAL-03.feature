Feature: TC-CAL-03
  Scenario: A successful refresh appends and auto-tags
    Given a daily fetch returning next year's FOMC schedule
    Then new events append with source and timestamp journaled
    And a standing "always block FOMC" rule auto-tags the new dates

  Scenario: A garbage fetch can never damage existing data
    Given a fetch that fails, parses empty, or returns 40 FOMC dates
    Then it is rejected whole, existing events are byte-identical, and one alert fires

  Scenario: A vanished date is disputed, never dropped
    Given a previously imported FOMC date absent from today's fetch
    Then the event is marked DISPUTED with an alert and its NO-TRADE tag stands

  Scenario: Feed failure is loud but never blocks trading
    Given cal_refresh_fail_alert_days consecutive failures
    Then a persistent alert raises and entries remain ungated by the calendar's absence
