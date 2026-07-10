Feature: TC-RPT-01
  Scenario: Period buckets and trust stamps
    Given fills across two ET days, one broker-reconciled and one pending
    Then Today shows only today's entries with a bot-computed badge
    And the month badge reads "1/2 days broker-confirmed"
    And paper fills never appear in live periods or exports

  Scenario: Disarmed flat days do not dilute averages
    Given 5 trading days and 2 disarmed flat days in a month
    Then day-based means and win rates use n=5
