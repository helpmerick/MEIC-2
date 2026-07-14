Feature: TC-RPT-18
  Scenario: A day with no journaled order ids is refused, never fabricated
    Given filled entries whose events carry no broker order id
    Then reconcile journals NOTHING, stamps nothing, and raises ONE critical alert
    And the broker side is never reported as zero
