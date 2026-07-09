Feature: TC-OWN-11
  Scenario: Pre-existing positions do not block arming or entries
    Given the broker account holds positions with no bot fills behind them
    When startup reconcile runs
    Then the positions are classified FOREIGN with a critical alert and persistent banner
    And arming succeeds and scheduled entries fire normally

  Scenario: A genuine shortfall still blocks
    Given the bot ledger records 2 contracts of a symbol and the broker reports 1
    Then a ReconciliationMismatch is logged and RSK-03 blocks entries until reconciled

  Scenario: Foreign-occupied strikes block both types
    Given a FOREIGN long at the put side's target strike
    When strike selection runs
    Then the strike is treated as blocked and the shift budget applies
    And a FOREIGN short at a candidate long strike also blocks (no stacking onto foreign lots)

  Scenario: max_day_risk counts only the bot's book
    Given foreign positions of any size and no open bot entries
    When an entry whose worst case fits max_day_risk is attempted
    Then RSK-04 passes — the foreign book does not consume the ceiling
    And the buying-power gate still evaluates broker reality including the foreign book

  Scenario: Never touch survives the whole day
    Given trading proceeds alongside FOREIGN positions all day
    Then no bot order ever references a foreign lot (OWN-04 caps at ledger)
    And EOD verification ignores foreign working orders it did not place
