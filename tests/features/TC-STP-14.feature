Feature: TC-STP-14
  Scenario: Markup raises the trigger in per_side basis
    Given stop_basis = per_side, stop_loss_pct = 95, stop_rebate_markup = 0.50
    Then the put stop trigger = round_to_tick(1.35 + 0.95*1.20 + 0.50)
    And the call stop trigger = round_to_tick(1.25 + 0.95*1.10 + 0.50)

  Scenario: Markup raises the trigger in total_credit basis
    Given stop_basis = total_credit and the same markup
    Then both triggers = round_to_tick(0.95*2.30 + 0.50)

  Scenario: Default markup of zero changes nothing
    Given stop_rebate_markup = 0.00
    Then triggers are byte-identical to the pre-markup formulas

  Scenario: NLE and calibration incorporate the markup
    Given a markup of 0.50 in force
    Then the NLE estimate is computed from the markup-inclusive trigger
    And the calibration record for a stop event stores markup = 0.50

  Scenario: UI worst-case disclosure
    Given the operator sets markup 0.50 in the UI
    Then the setting displays the worst-case increase before saving  # UI-18

  Scenario: Intraday change is next-entry only
    Given markup changed 0.00 -> 0.50 after entry 1 filled
    Then entry 1's resting stops are unchanged and entry 2 uses 0.50
