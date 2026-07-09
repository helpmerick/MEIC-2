Feature: TC-ORD-08
  Scenario: Net credit comes from the broker's fill, not the working limit
    Given a 4-leg entry limit working at net credit 3.50
    And the broker reports per-leg fill allocations: shorts 1.80 and 1.95, longs 0.08 and 0.07
    When the fill is recorded
    Then the entry's net credit is 3.60 (sum of allocated legs)
    And never the 3.50 working limit or any pre-fill estimate

  Scenario: Missing allocations are never fabricated
    Given the broker reports the fill without a usable per-leg allocation
    When the fill is recorded
    Then the order-level fill price is used for net credit
    And no per-leg price is ever fabricated (ORD-09; the STP-02d reconciliation record logs FAIL)
