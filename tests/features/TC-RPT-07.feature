Feature: TC-RPT-07
  Scenario: The waterfall reconciles to the cent
    Given a period with credits 8400, stop costs 2600, recoveries 310, buybacks 145, fees 220, slippage 95
    Then the waterfall bars sum exactly to the period net of 5650
    And premium capture ratio = 67.3 percent
    And any nonzero attribution residual renders an error state, never a silently adjusted bar
