Feature: TC-STP-18
  Scenario: per_side selection is rejected while the gate is in force
    Given config stop_basis = per_side, globally or on any entry override
    Then config validation rejects it with reason "allocation_unverified"
    And total_credit and short_premium remain selectable
    And no runtime toggle exists that lifts the gate

  Scenario: Allocation reconciliation is recorded on every real fill
    Given a condor fill from the live broker under any stop_basis
    Then a reconciliation record is logged comparing sum of allocated leg prices to the net fill
    And the record PASSES only if they agree within one tick and no leg is zero-priced without trading at zero
    And paper-mode fills never produce reconciliation records

  Scenario: Ungate criterion is fixed
    Given fewer than 5 consecutive PASSED reconciliation records from real fills
    Then the gate cannot be lifted
    And a FAILED record resets the consecutive count to zero
    And lifting the gate requires an operator-ratified spec amendment
