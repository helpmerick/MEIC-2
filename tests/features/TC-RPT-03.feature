Feature: TC-RPT-03
  Scenario: Outcomes classify exactly once and honor the v1.38 contract
    Given the 4.00-credit canonical trade stopped on the put side only
    Then the entry is ONE_SIDE_STOPPED with realized >= +20 dollars minus recorded slippage
    And a both-sides day classifies BOTH_SIDES_STOPPED with realized >= -360 dollars minus recorded slippage

  Scenario: A contract breach flags red
    Given a ONE_SIDE_STOPPED entry whose realized loss exceeds the recorded slippage allowance
    Then the dashboard renders a contract-breach flag with a drill-down to its fills
