Feature: TC-CAL-02
  Scenario: A manual fire on a tagged day warns and requires acknowledgment
    Given today is tagged NO-TRADE "FOMC" and the operator presses the manual fire button
    Then the OK dialog shows the blackout warning and OK stays disabled until acknowledged
    And an acknowledged fire proceeds, is evented, and reports tagged "blackout_overridden"
