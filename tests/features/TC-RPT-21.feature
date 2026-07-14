Feature: TC-RPT-21
  Scenario: Own-scoped broker figures render; absent them the fold renders
    Given an own-scoped correction of net 43.68 gross 50.00 fees 6.32
    Then the day report renders exactly those figures as authoritative
    And a day without one renders the plain fold byte-identical, badged bot-computed
