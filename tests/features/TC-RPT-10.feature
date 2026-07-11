Feature: TC-RPT-10
  Scenario: Imported days render cash and are excluded from quality metrics
    Given a backfilled day with fills netting +355.12 and an imported settlement of -369.00
    Then the day renders net -13.88 with the broker-imported badge
    And it appears in no Sharpe, expectancy, streak, outcome, targeting, or slippage figure
    And it counts as a trading day in period buckets

  Scenario: Transaction-level idempotency
    Given a day imported fills-only and then re-imported
    Then exactly the missing settlement rows are added once and a third run is a true no-op

  Scenario: Never CondorFilled, never foreign, never guessed
    Then imported rows are ExternalFillImported events only
    And only operator-listed order ids import; foreign fills never do
    And a settlement symbol shared with skipped-foreign fills is counted ambiguous_settlements and skipped
