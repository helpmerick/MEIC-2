Feature: TC-RPT-22
  Scenario: The order-id backfill moves no money and survives its own retraction
    Given a backfill of operator-supplied order ids followed by one retraction
    Then every money fold is byte-identical before and after both operations
    And re-running either is a no-op and the retracted id is never re-adopted
