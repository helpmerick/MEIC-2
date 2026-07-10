Feature: TC-RPT-09
  Scenario: A matching day is stamped broker-confirmed
    Given the day's projected fills, cash delta, fees, and flat check match the broker
    Then the day is stamped broker-confirmed and UI-25 shows the tick

  Scenario: A mismatch corrects to broker truth, never silently
    Given the broker reports fees 2.40 where the projection assumed 2.20
    Then a CorrectionRecord event enters the log storing both values and the diff
    And the dashboard renders the broker value with the correction visible in the drill-down
    And an alert fires and the RPT-08 correction count increments

  Scenario: No dashboard number ever changes without a CorrectionRecord
    Then any divergence between rendered numbers and the projection fold is a test failure

  Scenario: Broker unreachable never auto-confirms
    Given the EOD reconcile fetch fails
    Then the day remains bot-computed and reconciliation retries at the next boot or reconcile
