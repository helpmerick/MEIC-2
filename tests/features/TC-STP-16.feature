Feature: TC-STP-16
  Scenario: Vector 1 - the canonical 400-dollar contract
    Given shorts 3.00 + 2.00, wings 0.50 + 0.50, net credit 4.00, pct 95
    Then both triggers = 3.80 exactly
    And one side stopped with the other expiring nets +20
    And both sides stopped nets -360

  Scenario: Vector 2 - pct 100 boundary
    Given the same trade at pct 100
    Then both triggers = 4.00, one-side nets 0, both-sides nets -400 exactly

  Scenario: Vector 3 - floor rounding in the 0.10-tick regime
    Given net credit 3.60 at pct 95 (raw trigger 3.42)
    Then the trigger floors to 3.40, never 3.50

  Scenario: Vector 4 - floor rounding in the 0.05-tick regime
    Given net credit 3.10 at pct 95 (raw trigger 2.945)
    Then the trigger floors to 2.90, never 2.95

  Scenario: Vector 5 - markup spends the one-side guarantee (documented consequence)
    Given vector 1 plus stop_rebate_markup 0.50
    Then both triggers = 4.30
    And a one-side hit nets -30 plus long recovery   # the +20 guarantee is traded away by the dial
    And both sides nets -460

  Scenario: Vector 6 - feasibility kill
    Given shorts 3.00 + 2.00 with wings 1.50 + 1.50 (net credit 2.00, raw trigger 1.90)
    Then the trigger sits below the 3.00 short and the entry is SKIPPED "infeasible_stop"

  Scenario: Vector 7 - feasibility knife-edge
    Given net credit 3.37 at pct 95 (raw 3.2015, floors to 3.20) vs a 3.00 short
    Then clearance is exactly 2 ticks and the entry is FEASIBLE   # rule is >=

  Scenario: Regression guard - the corrected behavior can never silently return
    Given vector 1 with stop_basis = total_credit
    Then the trigger MUST be 3.80 and MUST NOT be 5.85
    # failure message: "per-leg (short_premium) default has crept back in"
