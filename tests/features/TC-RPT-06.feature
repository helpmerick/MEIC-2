Feature: TC-RPT-06
  Scenario: The reporting module cannot trade
    Then the reporting module has no order-action dependency on the broker gateway
    And no /reports endpoint can mutate trading state
    And its only broker access is the RPT-15 read-only reconciliation fetch
