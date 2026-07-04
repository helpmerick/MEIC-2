Feature: TC-TPF-01
  Scenario: Credit $4.00, profit $1.00 (25%)
    Then enabled levels are exactly {5, 10, 15, 20}
    And 25 and above are disabled with reason "too close - would trigger immediately"

  Scenario: Credit $4.00, profit $3.00 (75%)
    Then enabled levels are exactly {5, 10, ..., 70}
    And 75 and above are disabled

  Scenario: Profit 23%
    Then the highest enabled level is 15   # 20 violates the 5-point gap (23 - 20 < 5)
