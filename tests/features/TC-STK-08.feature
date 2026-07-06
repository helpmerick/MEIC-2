Feature: TC-STK-08
  Scenario: Vector A - first down-probe matches
    Given strikes with raw mids 3.20, 2.93, 2.70   # 2.93 rounds to probe price 2.95
    Then probes run 3.00 (miss), 2.95 (MATCH)
    And the 2.93 strike is sold

  Scenario: Vector B - up-probe within the cap matches
    Given strikes with raw mids 3.30, 3.05, 2.80
    Then probes run 3.00, 2.95, 3.05 (MATCH)
    And the 3.05 strike is sold

  Scenario: Vector C - equal distance above the cap is NEVER selected
    Given strikes with raw mids 3.45, 3.20, 2.80
    Then all seven probes 3.00 to 3.15 miss
    And the down-only phase matches 2.80
    And the 3.20 strike is never selected despite equal distance to target

  Scenario: Vector D - full exhaustion skips
    Given no strike's rounded mid lies between 1.75 and 3.15
    Then all 3 up-probes and all 25 down-probes miss
    And the entry is SKIPPED with reason "no_valid_strikes"

  Scenario: Vector E - deep walk sells thin but legal premium
    Given a strike with raw mid 1.80 and nothing nearer the 3.00 target
    Then the down-only phase matches at probe 1.80 (within the 25-step depth)
    And the strike is sold   # 1.80 >= the 1.00 hard floor

  Scenario: Vector E2 - the 1.00 hard floor beats the walk depth
    Given target 2.00 and the only match would be at raw mid 0.95
    Then the effective floor is max(2.00 - 1.25, 1.00) = 1.00
    And probes below 1.00 are never taken
    And the entry is SKIPPED with reason "no_valid_strikes"

  Scenario: Rounding lattice is nearest-0.05
    Given a strike with raw mid 2.92
    Then it answers probe 2.90, not 2.95   # 2.92 rounds down to 2.90

  Scenario: Probe order is deterministic and logged
    Then the exact sequence T, T-0.05, T+0.05, T-0.10, T+0.10, T-0.15, T+0.15,
         T-0.20, T-0.25 ... is enumerated verbatim
    And the day report records which probe number matched
