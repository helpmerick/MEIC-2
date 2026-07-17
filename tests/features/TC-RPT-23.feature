Feature: TC-RPT-23
  Scenario: The table shows today's entries from the one aggregation path
    Given two closed entries and one open entry today
    Then the Trading tab's table shows all three with per-side badges, credits, and realized P&L net of fees
    And the open row shows live P&L badged unrealized and updates in place
    And every figure matches the canonical aggregation byte-for-byte (no view-local recompute)

  Scenario: Unmanaged P&L is computed from recorded samples only
    Given an entry closed at 10:00 whose legs were sampled through 16:00
    Then its Unmanaged P&L = premium received minus the recorded 16:00 spread value
    And an entry with missing close-time samples renders "no data (not sampled)", never an interpolation

  Scenario: Sampling continues after close, day-scoped (D8b)
    Given an entry that closes mid-morning
    Then its legs keep receiving 1-minute samples until 16:00 and none after
    And the counterfactual never triggers any fetch of historical quotes

  Scenario: Provisional stays provisional
    Given a row held to expiry whose broker settlement has not landed
    Then its realized P&L renders the EOD-01 PROVISIONAL label, never fake finality
