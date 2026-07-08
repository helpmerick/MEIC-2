Feature: TC-ORD-07
  Scenario: Fill events record broker-reported symbols and allocations
    Given a condor fill is confirmed by the broker
    Then the fill event records, for each of the 4 legs, the broker-reported OCC symbol and allocated price
    And the recorded symbols are byte-identical to the broker payload

  Scenario: Every later order action uses the recorded symbol
    Given a recorded fill with leg symbols
    When a stop, LEX sell, decay buyback, close, or flatten order is built for a leg
    Then the order's instrument symbol is the recorded one
    And no code path reconstructs the symbol from strike and expiry at action time

  Scenario: Reconstruction only ever cross-checks
    Given a recorded symbol that disagrees with reconstruction from the condor's strikes
    Then an alert is raised naming both values
    And the recorded symbol is still the one used

  Scenario: Paper records simulator symbols identically
    Given a paper-mode fill
    Then the fill event carries simulator-assigned leg symbols in the same fields
