Feature: TC-STP-19
  Scenario: Trigger uses the actual net fill credit
    Given an entry filled at actual net credit 3.60 with stop_basis total_credit at 95 percent
    When protective stops are placed
    Then each trigger = floor_to_tick(0.95 * 3.60) = 3.40
    And never 95 percent of the 3.50 working limit or the pre-fill mid estimate
    And this agrees with TC-STP-16 vector 3 (3.42 floors to 3.40)
