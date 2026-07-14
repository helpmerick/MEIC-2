Feature: TC-RPT-20
  Scenario: Unscoped corrections are permanently inert
    Given a scope-less CorrectionRecord asserting broker cash of -534.46
    Then it never overrides the fold, never displays, and never counts as reconciled
    And an own-scoped record on the same day does all three
