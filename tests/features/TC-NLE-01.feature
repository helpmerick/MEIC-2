Feature: TC-NLE-01
  Scenario: Chain-implied estimate matches hand computation
    Given a scripted put-side chain:
      | strike | mid  |
      | 5990   | 1.35 |   # short
      | 5960   | 3.10 |
      | 5950   | 4.20 |
      | 5945   | 5.14 |   # = trigger => D = 45
      | 5940   | 0.15 |   # long (fill)
      | 5985   | 1.55 |   # long strike shifted 45 ITM = 5940+45
    And stop trigger = 5.14 and nle_haircut_pct = 30
    Then implied move D = 45
    And raw long estimate = 1.55, haircut estimate = 1.085
    And estimated net loss = (5.14 - 1.35) - (1.085 - 0.15) = 2.855
    And it is reported in $ and as % of the stop-basis credit
    # interpolation asserted separately with a trigger falling between strikes
