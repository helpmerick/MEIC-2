Feature: TC-OWN-01
  Scenario: Unmatched position is never touched
    Given the broker reports short 1 SPX 6050 call with no matching bot order fill
    Then the position is marked FOREIGN
    And the bot never submits any order referencing 6050 calls (stop, close, or hedge)
    And it appears in no bot P&L or risk figure
    And a critical alert and persistent banner are raised

  Scenario: Even a foreign naked short is alert-only
    Given the FOREIGN position is an unprotected naked short in a moving market
    Then the bot still submits no orders for it   # never guess operator intent
