Feature: TC-STP-04
  Scenario: Unconfirmed stop escalates to UNPROTECTED handling
    Given the broker rejects stop placement stop_retry_attempts times
    Then the affected side is flattened per unprotected_action
    And a critical alert is raised
    And total unprotected time <= stop_retry_seconds * stop_retry_attempts
