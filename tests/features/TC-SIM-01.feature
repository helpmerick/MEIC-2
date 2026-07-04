Feature: TC-SIM-01
  Scenario: Touch does not fill; trade-through does
    Given a condor limit at 2.30 net credit
    When the real net mid touches 2.30 exactly
    Then the order does NOT fill
    When the natural price satisfies 2.30 OR the mid reaches 2.35 (one tick through)
    Then the order fills all-or-nothing with per-leg prices allocated from current quotes
