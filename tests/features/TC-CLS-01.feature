Feature: TC-CLS-01
  Scenario: Manual close and TPF close are byte-identical
    Given two identical open entries A and B (same fills, same stops)
    When entry A is closed via the UI "Close trade" button
    And entry B is closed via a TPF floor trigger
    Then the sequence of broker requests (cancels, close orders, prices, quantities) is identical
    And only the recorded initiator differs: "manual" vs "take_profit"
