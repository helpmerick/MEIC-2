Feature: TC-OWN-03
  Scenario: Operator sells 1 more of the bot's short strike
    Given the bot is short 2 x 5990 put (ledger = 2, stops resting for 2)
    When the broker position becomes short 3 (foreign_delta = 1)
    Then a persistent shared-symbol warning is shown
    And the resting stops remain for exactly 2
    And a subsequent stop fill triggers LEX for exactly the bot's long quantity
    And a Close trade on that entry submits orders for exactly 2
