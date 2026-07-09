Feature: TC-RPT-02
  Scenario: The canonical five-day vector computes exactly
    Given capital base 10000 and daily nets +400, +20, -360, +400, +20
    Then ROC = 4.80 percent, annualized Sharpe = 4.79, max drawdown = 360 dollars (3.60 percent)
    And profit factor = 2.33, expectancy = +96 dollars per entry, day win rate = 80 percent

  Scenario: Sharpe gates on sample size
    Given 19 trading days
    Then Sharpe and Sortino render "insufficient data" and ROC still renders
