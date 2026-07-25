Feature: TC-CLS-07
  Scenario: A cancelled pre-fill entry leaves the open set
    Given a WORKING entry with nothing filled
    When Cancel entry confirms the broker cancel
    Then a terminal EntryClosed with initiator "cancelled" is journaled
    And the entry no longer appears open in the projection
    And Flatten All does not target it and does not warn about it again

  Scenario: A partial fill is never journalled as a plain cancel
    Given a cancel that discovers a filled balanced condor
    Then EC-ENT-06 applies (the fill is kept and protected) and no cancelled-terminal event is journaled

  Scenario: A superseded cancel is never reported as a clean cancel
    Given the ladder mints a new working order id mid-replace while the panel holds an older snapshot
    When the cancel targets the superseded id
    Then the result is "cancel_superseded", the current id is re-resolved, and the attempt retries within its bound
    And no report states "cancelled" for that press

  Scenario: Every terminal path journals a truthful initiator (v1.88, Finding C)
    Given an entry the ladder priced out at its credit floor
    Then it journals terminal with initiator "unfilled", never "cancelled"
    And an operator-cancelled ladder skip journals initiator "cancelled_by_operator"
    And a click landing before the first submit returns journals NOTHING until that submit's outcome is known
    And no phantom entry remains open in the projection, Flatten's targets, or the day-trades table

  Scenario: The day report never calls a cancel EXTERNAL (v1.88, Finding D)
    Given a cancelled entry and an unfilled entry
    Then they classify CANCELLED and UNFILLED respectively
    And neither is reported EXTERNAL (which asserts an operator action at the broker)
    And both stay excluded from strategy-quality metrics

  Scenario: cancelled is never treated as flat
    Then no close or flatten path may show green on "cancelled" alone
    And flatness must be proven from broker truth before any all-clear renders
