Feature: TC-CAL-05
  Scenario: Warnings appear day-of and T-1/2/3 trading days before, never blocking
    Given an FOMC event and event_warning_lead_days = 3
    Then a dismissable banner shows on the day and 1, 2, and 3 trading days before
    And no entry is ever blocked or gated by the warning
    And the countdown is measured in trading days so a weekend is skipped

  Scenario: Dismissal is per-event-per-tier and never pre-silences the nearest warning
    Given the operator dismisses the T-3 FOMC banner
    Then the T-2, T-1, and day-of FOMC banners still appear as the event approaches
    And re-dismissing a given tier never re-nags, across restarts (REC-07)

  Scenario: Warnings are honest and never fabricated
    Given a tier-2 Fed-speaker event
    Then its banner is labeled best-effort, never stated as certain
    And no banner ever appears for an event not on the calendar

  Scenario: A warning is not a tag
    Given an untagged OpEx day with its warning showing
    Then entries still fire normally (the warning informs, CAL-05 enforces only on tags)
