Feature: TC-RPT-05
  Scenario: Replay reproduces the dashboard
    Given any event log
    When the log is replayed from genesis into a fresh projection
    Then every dashboard number is byte-identical to the incremental projection
