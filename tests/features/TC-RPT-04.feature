Feature: TC-RPT-04
  Scenario: Targeting decomposition separates causes
    Given target 3.00, matched probe 2.95 at probe number 2, short filled 2.93
    Then selection gap = -0.05, execution gap = -0.02, probe depth = 2

  Scenario: Slippage-in can be positive
    Given first-rung credit 3.50 and fill credit 3.60
    Then slippage-in = +0.10 price improvement

  Scenario: Stop slippage reports from EC-STP-03 records
    Given a stop with trigger 3.80 filled at 3.90
    Then slippage-out = 0.10 = 2 ticks and it enters the mean and p90
