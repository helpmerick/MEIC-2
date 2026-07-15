Feature: TC-DOC-01
  Scenario: The guide renders from the spec with a version stamp
    Given the how-it-works tab
    Then it renders doc 12's content as single source stamped with the spec version it describes
    And a stamped-vs-running version mismatch renders a banner, never silent currency
    And every DOC-03 chapter is present (the completeness contract)
    And the tab carries no trading controls

  Scenario: Four-tab navigation (operator-specified)
    Then the SPA's top-level tabs are exactly Trading, Results, Calendar, How it works
