Feature: TC-STP-04
  Scenario: Unconfirmed stop escalates to UNPROTECTED handling
    Given the broker rejects stop placement stop_retry_attempts times
    Then the affected side is flattened per unprotected_action
    And a critical alert is raised
    And total unprotected time <= stop_retry_seconds * stop_retry_attempts

  Scenario: Stop quantity must equal the short position it protects
    Given an entry filled with contracts = 2
    When a stop is confirmed working with quantity 1
    Then the mismatch is detected at placement confirmation
    And the condition is handled as UNPROTECTED per STP-04
    And a critical alert names the naked quantity

  Scenario: Reconcile catches a quantity mismatch that arose later
    Given a working stop whose quantity no longer equals the short leg's ledger quantity
    When reconcile runs
    Then the entry is treated as UNPROTECTED (or OWN-10 if operator-resized)
    And the bot never silently resizes the stop itself
