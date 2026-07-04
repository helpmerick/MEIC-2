Feature: TC-ENT-02
  Scenario Outline: Pre-entry gate blocks entry
    Given <gate_condition> is true at 10:30 ET
    Then entry 2 is SKIPPED with reason <reason>
    And no order is submitted
