Feature: TC-STP-15
  Scenario: Thin credit is skipped before entry
    Given estimated net credit 2.00 at 95% (trigger 1.90) and the short put mid is 3.00
    Then the entry is SKIPPED with reason "infeasible_stop" and no order is submitted

  Scenario: Healthy credit passes
    Given estimated net credit 4.00 (trigger 3.80) vs shorts at 3.00 and 2.00
    Then triggers clear both fills by the minimum distance and stops are placed

  Scenario: Post-fill infeasibility closes instead of placing a suicidal stop
    Given fills land such that the actual trigger does not clear a short's fill
    Then no stop is placed for that entry
    And the entry closes via CLS-01 with initiator "infeasible_stop" and an alert

  Scenario: Markup counts toward feasibility
    Given a rebate markup that lifts the trigger above fill + minimum distance
    Then the entry is feasible   # STP-02b adds to the trigger before the check
