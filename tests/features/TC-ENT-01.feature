Feature: TC-ENT-01
  Scenario: Entry executes inside its window
    Given the clock reaches 10:00:00 ET
    When the entry attempt begins within entry_window_seconds
    Then a 4-leg condor limit order is submitted per ORD-01/ORD-02

  Scenario: Missed window is never executed late
    Given the bot was down from 09:55 to 10:05 ET
    When the bot restarts at 10:05
    Then entry 1 is marked SKIPPED with reason "missed_window"
    And no order for entry 1 is ever submitted
