Feature: TC-RSK-07
  Scenario: Crash with open positions
    Given one open condor, one side mid-LEX, one working entry order
    When the process is killed and restarted
    Then within recovery_sla_seconds every short is covered by a confirmed resting stop
    And the LEX ladder has resumed
    And the stale entry order is cancelled (window elapsed)
    And zero duplicate orders exist at the broker
