Feature: TC-DOC-01
  Scenario: The guide renders from the spec with a version stamp
    Given the how-it-works tab
    Then it renders doc 12's content as single source stamped with the spec version it describes
    And a stamped-vs-running version mismatch renders a banner, never silent currency
    And the master flowchart is clickable to a full-screen pannable zoomable view (v1.77)
    And every DOC-03 chapter is present (the completeness contract)
    And the tab carries no trading controls

  Scenario: Five-tab navigation (operator-specified, v1.75)
    Then the SPA's top-level tabs are exactly Trading, Results, Calendar, How it works, Getting started

  Scenario: Getting-started never leaks a secret (DOC-06/UI-32)
    Then the tab renders variable NAMES and explanations only
    And no current env value, password, token, or secret ever renders anywhere in it
    And all five DOC-06 sections are present (the completeness contract)
