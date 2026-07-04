Feature: TC-ORD-01
  Scenario: Reprice ladder walks down and respects the floor
    Given the entry order does not fill
    When entry_reprice_seconds elapses 5 times
    Then the limit was repriced down one tick each time
    And never below min_total_credit
    And after the final attempt the order is cancelled and entry SKIPPED "unfilled_at_floor"
