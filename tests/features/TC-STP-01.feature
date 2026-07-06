Feature: TC-STP-01
  Scenario: Stops placed immediately on fill (total_credit basis - THE DEFAULT, Ash's outcome contract)
    Given stop_basis = total_credit
    When the condor fill is confirmed
    Then two buy-to-close stop-market orders (TIF Day) are working within the same processing turn
    And each trigger price = floor_to_tick(0.95 * 2.30)   # -> 2.15, not 2.20
    And no stop exists on either long leg   # STP-06

  Scenario: The outcome contract holds (Ash's Way 2, the 400-dollar example)
    Given net credit 4.00, both stops at 3.80, longs recover zero
    Then one side stopped and one side expiring nets +0.20 (small profit, the kept 5%)
    And both sides stopped nets -3.60 (about the premium, never more before slippage)

  Scenario: Stops placed immediately on fill (short_premium basis, selectable per entry)
    Given stop_basis = short_premium
    Then the put stop trigger = floor_to_tick(1.35 * (1 + 0.95))   # -> 2.60
    And the call stop trigger = floor_to_tick(1.25 * (1 + 0.95))   # -> 2.40
    And neither trigger depends on any long leg's allocated fill price

  Scenario: Stops placed immediately on fill (per_side basis)
    Given stop_basis = per_side
    When the condor fill is confirmed
    Then the put stop trigger = floor_to_tick(1.35 + 0.95 * 1.20)   # -> 2.45
    And the call stop trigger = floor_to_tick(1.25 + 0.95 * 1.10)   # -> 2.25
    And side net credits are computed from the broker's allocated leg fills
