Feature: TC-UI-05
  Scenario: ET times echo in the operator's local zone
    Given the operator's browser zone is Europe/London
    When a row's ET time is 11:53
    Then "16:53 London" (approx) renders beneath the cell
    And DST is tracked automatically per instant
    And an invalid time shows the precise rejection reason instead of an echo
