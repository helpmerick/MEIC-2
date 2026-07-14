Feature: TC-STP-21
  Scenario: The effective stop percentage is displayed and caged
    Given credit 2.80 and markup 0.30 with max_effective_stop_pct = 110
    Then the trigger floors to 2.95 and the display shows effective 105.4 percent — allowed
    And on credit 2.00 the same markup shows 110 percent — allowed at the boundary
    And a combination exceeding the cap skips with reason "markup_exceeds_cap", never clamped
