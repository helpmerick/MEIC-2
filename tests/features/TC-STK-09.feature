Feature: TC-STK-09
  Scenario: Dead-at-baseline strikes never count as holes
    Given warm-up validates 24 of 28 reachable strikes (4 far wings listed but never quoted)
    And at fire time 23 of the 24 validated strikes are still fresh
    Then completeness = 95.8 percent and the gate PASSES
    And under the pre-v1.55 rule the same day would have falsely skipped at 85.7 percent

  Scenario: A genuine feed regression still fails
    Given warm-up validated 24 strikes and only 12 remain fresh at fire time
    Then the gate fails and the entry retries then skips incomplete_chain

  Scenario: A sliver baseline cannot trivially pass
    Given warm-up finds only 5 validated strikes on the call side with min_validated_strikes = 10
    Then a warm-up alert fires 60 seconds before the window and the entry retries
    And an unhealed baseline skips incomplete_chain

  Scenario: A dead wing is a candidate skip, not an entry failure
    Given a candidate short whose wing strike is not in the validated universe
    Then that candidate is skipped and the probe walk continues
    And the entry fails only if no valid candidate remains

  Scenario: Manual entries baseline at press
    Given the operator fires manually with no warm-up
    Then the validated universe is captured at press time under the same rules
