Feature: TC-ENT-08
  Scenario: Manual fire passes every gate except the window
    Given the operator presses the manual fire button at 10:07, outside any scheduled window
    And all ENT-03 gates pass
    When the OK-confirmation dialog is acknowledged
    Then exactly one entry attempt runs through the identical pipeline
    And the entry is recorded with initiator "manual_entry"
    And no entry-count cap blocks it (ENT-05 retired v1.81); only RSK-04 and the order cap bound the day

  Scenario: No fire without the OK dialog
    Given the operator presses the manual fire button
    When the dialog is dismissed or times out
    Then no order is submitted and no attempt is recorded

  Scenario: Gates are never bypassed
    Given Stop Trading is ON
    When the operator presses the manual fire button and acknowledges OK
    Then the attempt is refused with skip reason "blocked" shown on the card

  Scenario: RSK-04 vetoes a manual entry like any other
    Given open entries whose summed worst case leaves less headroom than the manual entry needs
    When the manual entry is confirmed
    Then it is skipped with reason "max_day_risk"

  Scenario: Double-click is one attempt
    When the operator presses the button twice and confirms once
    Then exactly one order exists (idempotency key per press-confirmation)
