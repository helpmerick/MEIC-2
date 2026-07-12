Feature: TC-LEX-10
  Scenario: An intrinsic floor rests when the book is empty but spot is fresh
    Given a long P7510 with no bid, SPX at 7480, and lex_quote_wait_seconds elapsed
    Then a limit sell rests at 30.00 (intrinsic floored to tick)
    And the one-time critical alert fires when the floor order is placed

  Scenario: Quote resumption supersedes the floor
    Given the resting floor order and a usable bid arriving
    Then the raced-fill-guarded cancel/replace resumes normal ladder pricing

  Scenario: No bid and no spot defers honestly
    Given neither a bid nor a fresh underlying mark
    Then the side defers with the one-time critical alert and no price is ever invented

  Scenario: A resting floor survives a restart (v1.64)
    Given a resting intrinsic-floor sell and a bot restart
    Then boot re-adopts the order via its lex-floor idempotency key
    And a later usable quote supersedes it normally
    And a fill after restart classifies LongSold, never OWN standdown

  Scenario: Floor-filled sides report an honest gap
    Given a side closed via the intrinsic floor with no bid at stop time
    Then the long-recovery report shows "no mark (no bid)" and never a fabricated baseline

  Scenario: A floor that fills while the bot is down is recognized on boot (v1.65)
    Given a resting floor order that FILLED during a bot outage
    When the bot boots and the order is absent from working orders with a broker fill record
    Then LongSold is synthesized at the broker-actual price and the side closes terminally
    And no OWN-standdown misclassification occurs and the EOD audit counts the event
    And a floor gone WITHOUT a fill takes the OWN external-cancel path instead
