Feature: TC-RPT-19
  Scenario: A settlement symbol shared with a foreign fill is never guessed
    Given a symbol appearing on both an own fill and a skipped foreign fill that day
    Then its settlement row is excluded and counted in ambiguous_settlements
