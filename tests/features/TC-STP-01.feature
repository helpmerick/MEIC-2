Feature: TC-STP-01
  Scenario: Stops placed immediately on fill (short_premium basis - Rob's formula, default)
    Given stop_basis = short_premium
    When the condor fill is confirmed
    Then the put stop trigger = round_to_tick(1.35 * (1 + 0.95))
    And the call stop trigger = round_to_tick(1.25 * (1 + 0.95))
    And neither trigger depends on any long leg's allocated fill price

  Scenario: Stops placed immediately on fill (total_credit basis)
    Given stop_basis = total_credit
    When the condor fill is confirmed
    Then two buy-to-close stop-market orders (TIF Day) are working within the same processing turn
    And each trigger price = round_to_tick(0.95 * 2.30)
    And no stop exists on either long leg   # STP-06

  Scenario: Stops placed immediately on fill (per_side basis)
    Given stop_basis = per_side
    When the condor fill is confirmed
    Then the put stop trigger = round_to_tick(1.35 + 0.95 * 1.20)
    And the call stop trigger = round_to_tick(1.25 + 0.95 * 1.10)
    And side net credits are computed from the broker's allocated leg fills
