Feature: TC-STP-17
  Scenario: Silent in the normal world
    Given the mark crosses the trigger and the broker stop fills within 6 seconds
    Then the watchdog never alerts and never acts

  Scenario: Alert at grace, escalate at bound
    Given the mark holds at or above trigger and the resting stop stays unfilled
    Then a critical alert fires at 10 seconds
    And at 20 seconds the bot sends a marketable buy-to-close and cancels the resting stop
    And the side proceeds SIDE_STOPPED into LEX with initiator watchdog_escalation

  Scenario: Race - broker stop fills during escalation
    Given the resting stop fills while the escalation order is in flight
    Then the escalation aborts per ORD-08 and exactly one buy-back exists (order count = 1)

  Scenario: Stale marks pause the clock
    Given quotes go stale mid-breach
    Then the watchdog clock pauses and resumes on fresh data; no action on stale marks

  Scenario: Every escalation is calibration evidence
    Then each watchdog_escalation record stores mark-at-breach, elapsed time, and fill price
