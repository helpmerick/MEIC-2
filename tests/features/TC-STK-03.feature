Feature: TC-STK-03
  Scenario: Put side credit below minimum skips the whole entry
    Given the short put's mid = 0.80 and min_short_premium = 1.00
    Then the entry is SKIPPED with reason "insufficient_credit"
    And no order of any kind is submitted   # single-side entries prohibited
