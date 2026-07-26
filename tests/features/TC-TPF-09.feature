Feature: TC-TPF-09
  Scenario: A sub-minute breach is caught (regression guard for the 60s defect)
    Given an armed floor of 20 percent on an entry with net credit 4.00
    And profit falls below the floor at t=0ms and recovers at t=700ms
    Then evaluations occur at t=0, 250 and 500ms
    And CloseEntry runs with initiator "take_profit" at t=500ms

  Scenario: A breach shorter than the confirmation duration does not fire
    Given profit falls below the floor at t=0ms and recovers at t=300ms
    Then no close fires and the elapsed breach time is cleared on the recovery

  Scenario: An invalid evaluation CLEARS the elapsed time, never pauses it
    Given profit is below the floor from t=0ms onward
    And the evaluation at t=250ms is invalid
    Then no close fires at t=500ms
    And the close fires at t=1000ms

  Scenario: Confirmation of zero fires on the first valid breach
    Given tp_confirmation_ms is 0 and profit is below the floor at t=0ms
    Then CloseEntry runs with initiator "take_profit" at t=0ms

  Scenario: The projection is not re-folded per evaluation (TPF-03c)
    Given an armed floor and an event log that does not change
    When 20 evaluation passes run
    Then the projection fold is invoked at most once

  Scenario: An armed floor that cannot be evaluated is surfaced (TPF-03d)
    Given an armed floor whose entry has an open side that cannot be fully marked
    When that condition persists for exit_unevaluable_alert_s
    Then an RSK-06 critical alert names the entry and the reason
    And the card shows the exit as unevaluable, distinct from armed-and-healthy

  Scenario: A throwing evaluator alerts, it does not merely log (NFR-08a)
    Given the exit evaluation pass raises
    Then a CRITICAL alert is raised naming the error
    And repeat alerts for the same error are rate-limited to one per exit_unevaluable_alert_s
    And the evaluation loop survives the exception

  Scenario: Asymmetric freshness, conservative-only (TPF-03f)
    Given a short leg mark fresher than max_quote_age_ms and a long leg mark 20 seconds old
    Then the entry is evaluable and the stale long is taken at its CONSERVATIVE value
    And the evaluation records that a stale long mark was used
    And a short leg staler than max_quote_age_ms makes the entry unevaluable
    And a long leg older than exit_long_leg_max_age_ms makes the entry unevaluable

  Scenario: One entry's failure never blinds the others (TPF-03g)
    Given entries A, B and C each with an armed floor, all breached
    And evaluating A raises
    Then B and C are still evaluated in that same pass and both fire
    And the failure is alerted naming entry A specifically

  Scenario: The retired count key is rejected
    Given a config containing tp_confirmation_evals
    Then the config loader REJECTS it
