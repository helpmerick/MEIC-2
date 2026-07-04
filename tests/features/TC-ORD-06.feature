Feature: TC-ORD-06
  Scenario: Terminal cancel failure never retries (the all-night spam bug)
    Given a resting stop whose cancel fails with "order no longer exists"
    Then the order is marked dead and tracking stops
    And the cancel is never retried and protection is never re-added for a dead order
    And total requests for that order after the terminal response = 0

  Scenario: Transient failure retries bounded, filled routes to fill handling
    Given cancels failing with timeouts, then a cancel rejected because filled
    Then the timeout case retries with backoff up to its cap
    And the filled case is handled as a fill (EC-API-06)

  Scenario: Unclassifiable failure escalates
    Given a cancel failure matching no known class
    Then it is treated as transient with a hard retry cap and raises an alert at the cap
