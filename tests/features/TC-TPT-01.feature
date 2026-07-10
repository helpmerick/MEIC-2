Feature: TC-TPT-01
  Scenario: Target fires on the way up through the canonical close
    Given an entry with actual net credit 4.00 and take-profit target 60 percent
    When whole-entry profit holds at or above 60 percent for 2 consecutive valid evaluations
    Then CloseEntry runs with initiator "take_profit_target"
    And the order sequence is identical to a manual close of the same position

  Scenario: A passed target is rejected, never acted on
    Given a live entry currently up 35 percent
    When the operator submits a target of 30 percent
    Then it is REJECTED with "target already passed - current profit 35%"
    And 40 percent is the lowest selectable target

  Scenario: The target disarms permanently when any stop fills
    Given credit 4.00, target 5 percent, and the put stop fills at 3.80
    And the long put recovers 0.30 and the call side is closable for 0.20
    Then whole-entry profit is +30 dollars = 7.5 percent and NO close fires
    And the card shows the target as disarmed and the call side rides its resting stop

  Scenario: Armed feedback shows dollars
    Given actual net credit 4.00 and target 60 percent
    Then the card shows "closes at debit <= 1.60" and "keep >= 240 dollars"

  Scenario: Floor and target coexist
    Given a floor at 20 percent and a target at 70 percent on one entry
    Then rising to 70 first closes with initiator "take_profit_target"
    And falling to 20 first closes with initiator "take_profit"

  Scenario: Never broker-resting
    Then no resting take-profit order ever exists at the broker
    And each short leg has at most ONE working buy order at all times

  Scenario: Recovery order of operations
    Given the bot restarts on an entry whose put stop filled while it was down
    Then the synthesized stop event disarms the target BEFORE any target evaluation
    And a stop-free entry above target on recovery closes immediately
